# S6B.4C Shared-Store Concurrency Specification

## Status

```text
SPEC_STATUS=OWNER_AUTHORIZED_IN_PROGRESS
OWNER_AUTHORIZATION_DATE=2026-08-29
CURRENT_SLICE=S6B_4C_05_ASYNC_EMBEDDING_CONCURRENCY_AND_PINNED_BACKEND_RUNTIME_PROOF
CURRENT_SLICE_AUTHORIZATION_DATE=2026-08-30
CURRENT_SLICE_PHASE=DESIGN_AND_RED_ONLY
```

This specification defines the bounded S6B.4C campaign. It is not authorization to begin a later slice. Each slice has its own implementation and verification gate.

## Goal

Prove that FactLane can be operated by separate execution contexts against a shared disposable store without confusing tool presence with capability, leaking credentials, accepting an unbound host identity, losing updates, or silently degrading the pinned backend/provider contract.

## Campaign boundary

The campaign is limited to these slices, in order:

1. **4C-01 — Execution-context entry preflight and harness foundation**
2. **4C-02 — Transport-bound host identity and shared gateway**
3. **4C-03 — Atomic multi-client revision/CAS and lost-update prevention**
4. **4C-04 — Codex/Hermes disposable shared-store concurrency**
5. **4C-05 — Async embedding concurrency and pinned-backend runtime proof**
6. **4C-06 — Process/crash injection and final acceptance**

4C-01, 4C-02, 4C-03, and 4C-04 are CLOSED_PASS. The current implementation/acceptance slice is 4C-05 only. 4C-05 must not expand into process kill, cancellation cleanup, crash injection, lifecycle work, live host configuration, native memory, or production migration.

```text
4C-01=CLOSED_PASS
4C-02=CLOSED_PASS
4C-03=CLOSED_PASS
4C-04=CLOSED_PASS
4C-05=CURRENT
4C-06=NOT_STARTED
```

## Explicitly out of scope

The following belong to later phases and must remain untouched in S6B.4C:

- retention, compaction, reclaim, archive/recovery, or lifecycle housekeeping (S6B.4D);
- real/native memory bootstrap, production registration, or real user-memory migration (S6B.5);
- legacy-memory or Knowledge mutation;
- live Codex or Hermes configuration mutation;
- global MCP registration;
- backend pin, lockfile, embedding model, or accepted S6B.4B result changes.

## 4C-01 contract: execution-context preflight

The reusable preflight reports these fields without exposing credentials or command output:

```text
ACTOR
EFFECTIVE_HOME
OWNER_OR_REAL_HOME
REPOSITORY_ROOT
CURRENT_BRANCH
GIT_BINARY
GH_BINARY
AMBIENT_GITHUB_AUTH_READY
OWNER_CONTEXT_GITHUB_AUTH_READY
SIDE_EFFECT_CONTEXT
```

The report also states the contract verdicts:

```text
TOOL_PRESENT_VS_READY_CONTRACT
READINESS_EXACT_CONTEXT
ENVIRONMENT_SCOPED_FACT
EXECUTION_CONTEXT_PREFLIGHT
```

### Context rules

- `EFFECTIVE_HOME` is derived from the process environment used for the probe. It is never replaced by a guessed owner path.
- `OWNER_OR_REAL_HOME` is accepted only when supplied explicitly by the caller. Missing, non-directory, or unreadable owner context fails closed.
- `REPOSITORY_ROOT` and `CURRENT_BRANCH` are read from the current repository using bounded, argument-list Git commands. A missing repository or detached/unknown branch is not promoted to a valid execution context.
- `GIT_BINARY` and `GH_BINARY` describe binary presence only. A present binary is not a readiness result.
- `AMBIENT_GITHUB_AUTH_READY` probes GitHub auth with the effective runtime `HOME` and suppresses stdout/stderr. A failure is classified in that context, for example `GITHUB_AUTH_UNAVAILABLE_IN_HERMES_RUNTIME_CONTEXT`.
- `OWNER_CONTEXT_GITHUB_AUTH_READY` probes the same auth command with the explicitly supplied owner `HOME`, without copying credentials, keyrings, or config files. A failure is classified as owner-context failure, never as a global GitHub outage.
- The auth subprocess receives only the explicit `HOME`, safe executable/locale variables, and fail-closed flags; ambient XDG, Git-config, SSH-agent, and token variables are not inherited.
- `SIDE_EFFECT_CONTEXT` is `EXPLICIT_OWNER_CONTEXT` only after the explicit owner probe succeeds. Otherwise it is `APPROVED_BRIDGE_REQUIRED` and no owner-scoped side effect is allowed by this slice.
- `EXECUTION_CONTEXT_PREFLIGHT=PASS` means the repository context and the selected explicit Owner side-effect context are ready. An ambient runtime auth failure remains an environment-scoped fact and blocks only ambient-context side effects; it is not promoted to global unavailability.
- The report contains no token, secret, authorization header, subprocess output, or credential material. Readiness is recomputed in the exact context of each operation.

### Required negative behavior

The preflight must fail closed for:

- missing or invalid repository context;
- missing or invalid explicit owner context;
- missing Git/GitHub binaries;
- auth unavailable in the selected side-effect context; ambient auth failure remains scoped and blocks only ambient-context side effects;
- any attempt to treat binary presence as authenticated capability;
- any attempt to infer owner authority from ambient `HOME`.

## 4C-01 contract: native-host-memory interim gate

S6B.4C-01 performs no native-host-memory write and does not read memory contents. Before any future native-memory mutation, the project must run a read-only capacity/status check:

```text
CHECK_NATIVE_MEMORY_CAPACITY_BEFORE_MEMORY_MUTATION=REQUIRED
```

The gate may consume a real host-provided read-only capacity/status primitive when one exists. It must not invent a capacity estimate, inspect memory contents, compact data, or perform housekeeping. If no trustworthy primitive is available:

```text
CAPACITY_INTROSPECTION=UNAVAILABLE
FUTURE_NATIVE_MEMORY_MUTATION=FAIL_CLOSED_PENDING_CAPACITY_CHECK_OR_HOUSEKEEPING
```

A capacity result, even when available, does not authorize a native-memory write in 4C-01. S6B.4D owns the complete housekeeping lifecycle.

## Harness requirements

The 4C-01 harness is portable and disposable:

- Python implementation and pytest tests, not shell-only logic;
- temporary Git repositories and temporary store paths in tests;
- no `/home/<owner>`, Hermes-private path, credential, keyring, or machine-specific literal in tracked tests;
- no network dependency for unit tests; auth probing is injected in unit tests and exercised once as a bounded local command during acceptance;
- raw command output and acceptance evidence stay under `.factlane-local/evidence/s6b4c-01/` and are ignored;
- no database or native-memory state outside temporary test fixtures.

## Acceptance criteria for 4C-01

- The exact execution context is reported with the required fields.
- Tool presence and readiness are separate and tested independently.
- Ambient and explicit owner probes can produce different results for the same binaries.
- Auth failures are context-scoped and never claim global unavailability.
- Reports contain no credential material or probe output.
- Missing/invalid context is fail-closed.
- The native-memory capacity gate is explicit, read-only, and fail-closed when unavailable.
- `uv.lock`, backend pin, embedding model, live configuration, MCP registration, and memory contents are unchanged.
- The current slice is recorded in `TASKBOARD.md`; S6B.4C is in progress, but S6B.4D, S6B.5, and S6C remain blocked/not started as specified.

## Verification contract

The minimum 4C-01 verification is:

```text
git diff --check
uv run pytest -q
uv run python -c "import factlane; print(factlane.__name__)"
uv run factlane --help
NO_SECRET=PASS
NO_PRIVATE_ABSOLUTE_PATH_IN_TRACKED_FILES=PASS
OWNER_CONTEXT_BOARD_TRACKED=NO
NATIVE_MEMORY_MUTATION=NONE
LIVE_CONFIG_MUTATION=NONE
GLOBAL_MCP_REGISTRATION=NONE
```

A passing 4C-01 gate authorizes neither the next campaign slice nor any native-memory mutation.

## 4C-02 contract: transport-bound host identity and shared gateway

4C-02 establishes one project-neutral gateway boundary between a selected transport and
the existing `MemoryAdapter`. It proves request/transport identity separation and an
immutable per-gateway binding. It does not claim cryptographic principal authentication.

The authoritative binding is created at trusted gateway/server construction, outside the
MCP request payload:

```text
HostBinding (immutable)
  BOUND_HOST_ID=<explicit trusted launcher value>
  TRANSPORT_KIND=<selected transport implementation, currently stdio>
  GATEWAY_INSTANCE_ID=<internally generated per gateway instance>
  BINDING_SOURCE=<bounded non-secret construction source>
```

The binding rules are:

- `BOUND_HOST_ID` is explicit trusted launcher configuration only; it is never derived
  from `HOME`, hostname, cwd, username, credentials, or request data.
- `TRANSPORT_KIND=stdio` comes from the server's selected transport path, not a request
  field. A future transport must bind its own implementation kind.
- `GATEWAY_INSTANCE_ID` is generated internally and remains immutable for that gateway.
- Missing, empty, non-string, oversized, newline/control-bearing, or sensitive binding
  values fail closed. No token, credential path, environment dump, or full config path
  may enter a binding or audit envelope.
- `BOUND_HOST_IDENTITY` and `MEMORY_SCOPE_AGENT_ID` are separate domains. The gateway
  neither rewrites nor authorizes the adapter's `scope.agent_id`.

The gateway rejects, rather than ignores, top-level request identity claims including
`host_id`, `bound_host_id`, `host_identity`, `transport_identity`,
`gateway_instance_id`, and `runtime_agent_id`. Stable bounded errors are
`HOST_IDENTITY_CLAIM_DENIED`, `HOST_TRANSPORT_IDENTITY_MISMATCH`, or
`UNBOUND_GATEWAY`; the implementation must use these names consistently.

A gateway without a valid immutable binding cannot dispatch an operation. A successful
adapter response receives an audit projection such as:

```text
audit.host_binding = {
  host_id: <bounded non-secret bound value>,
  transport: "stdio",
  gateway_instance_id: <bounded opaque value>,
  binding_source: <bounded non-secret value>
}
```

The projection is gateway-owned, immutable per instance, and never copied from request
metadata. The public MCP tool set remains exactly the five adapter operations:
`memory_search`, `memory_get`, `memory_store`, `memory_update`, and `memory_status`.
No identity/admin/login/registration tool is added. Existing disposable MCP acceptance probes must pass an explicit non-secret host id; they may not rely on a HOME, hostname, cwd, or request-derived fallback.

### 4C-02 disposable proof and acceptance

Use two separately bound gateway instances (`codex-disposable` and
`hermes-disposable`) against one disposable FactLane store. Prove sequentially that A's
committed record is read by B, B's response audit identifies B, A's response audit
identifies A, and the requested memory scope including `scope.agent_id` remains unchanged.
This is `POST_COMMIT_VISIBILITY=PASS_SEQUENTIAL_FOUNDATION` only.

It must not be reported as `MULTI_CLIENT_CONCURRENCY`, `ATOMIC_CAS`, or
`LOST_UPDATE_PREVENTION`; no simultaneous writers, locks, CAS rewrite, crash injection,
or concurrent write campaign are permitted in 4C-02.

Mandatory tests cover unbound startup/dispatch, request identity denial and mismatch,
immutable bindings, distinct gateway IDs, host/scope-agent separation, audit provenance,
sequential shared-store visibility, exactly five public tools, secret/config/environment
exclusion, and the carried `MISSING_GIT_BINARY_EXPLICIT_REGRESSION_TEST` from 4C-01.

### 4C-02 TDD and verification boundary

Write the gateway tests before production implementation and observe the expected RED
failures. Then implement the smallest gateway/server seam over the existing adapter.
The required checks are:

```bash
git diff --check
uv run ruff check .
uv run pytest tests/unit/test_execution_context.py -q
uv run pytest tests/unit/test_gateway.py -q
uv run pytest -q
uv run python -c "import factlane; print(factlane.__name__)"
uv run factlane --help
```

The 4C-02 evidence root is `.factlane-local/evidence/s6b4c-02/` and remains ignored.
No native memory, live Codex/Hermes configuration, global MCP registration, backend pin,
embedding identity, schema, or `uv.lock` may change.

## 4C-03 contract: atomic multi-client revision/CAS and lost-update prevention

4C-03 closes the gap between the adapter's early sequential `expected_revision` check and
an actual atomic compare-and-swap decision across independent clients. The public
`memory_update` API remains unchanged. The authoritative CAS decision belongs inside the
FactLane storage transaction, not in a process-local `asyncio.Lock` and not in a new
coordinator service.

For an update carrying `supersede_record_id`, `SQLiteVecEngine.write_record` must:

1. acquire the existing write transaction with `BEGIN IMMEDIATE` through the pinned
   backend retry boundary;
2. before inserting any adapter/native/vector row, read the expected parent inside that
   transaction;
3. require that the expected parent exists and is still `VALIDATED_CURRENT`;
4. require that the new record's `parent_record_id` matches that same parent;
5. otherwise raise deterministic `VERSION_CONFLICT` and roll back the transaction;
6. only after the check succeeds, write the new record/vector and supersede the parent in
   the same transaction.

The first writer from a shared current parent therefore wins. Any later writer that was
prepared from the same parent loses with `VERSION_CONFLICT`; it must leave no partial
adapter row, native memory row, vector row, or competing current lineage branch.

The rule applies to both existing update modes:

- `REVERIFY`: the logical `memory_id` is preserved, revision advances once, and history
  remains a single linear chain;
- `REPLACE`: the replacement receives a new logical `memory_id` while the old current
  parent is superseded exactly once and only one replacement becomes current.

FactLane must not add duplicate SQLite lock/backoff logic. WAL, `busy_timeout`, and
bounded `locked`/`busy` retry remain owned by the exact pinned backend. No schema,
backend pin, lockfile, embedding identity, public tool, or gateway identity contract is
changed by 4C-03.

### 4C-03 deterministic disposable proof

Use two separately constructed `SQLiteVecEngine`/`MemoryAdapter` clients with independent
connections to one disposable database. Both clients must complete their read of the
same current revision before either storage write proceeds. A test-only synchronization
barrier at the write boundary makes this precondition deterministic rather than relying
on scheduler timing.

Required assertions for both `REVERIFY` and `REPLACE` are:

```text
INDEPENDENT_CLIENT_CONNECTIONS=2
SHARED_PRE_READ_PARENT=YES
SUCCESSFUL_WRITERS=1
VERSION_CONFLICT_WRITERS=1
CURRENT_LINEAGE_FORKS=0
PARTIAL_LOSER_ROWS=0
CURRENT_RECORD_COUNT=1
LOST_UPDATE_PREVENTION=PASS
```

The proof is limited to independent in-process clients/connections over the real pinned
SQLite-vec storage boundary. Real Codex/Hermes execution-context concurrency belongs to
4C-04. Async embedding contention belongs to 4C-05. Process/crash injection belongs to
4C-06.

### 4C-03 TDD and verification boundary

The RED test must fail on the pre-4C-03 implementation because both writers can succeed
from one shared parent. The GREEN implementation is the smallest transaction-local CAS
check described above. Before PR handoff, verify the exact final branch head with the
repository CI equivalent:

```text
uv sync --frozen --dev
uv run pytest -q
uv run python -c "import factlane; print(factlane.__name__)"
uv run factlane --help
```

Also verify by diff inspection that `uv.lock`, backend pin, embedding identity, schema,
live Codex/Hermes configuration, native memory, global MCP registration, and later-slice
implementation are unchanged.

## 4C-04 contract: Codex/Hermes disposable shared-store concurrency

4C-04 lifts the accepted 4C-03 single-winner CAS semantics across the execution-context
boundary without changing the public API or adding coordination infrastructure. The
proof must use one disposable store and two separately launched actors bound as
`codex-disposable` and `hermes-disposable`.

Two evidence levels are mandatory and must remain distinct:

```text
PROCESS_BOUNDARY_CI_PROOF=REQUIRED
REAL_CODEX_HERMES_EXECUTION_CONTEXT_PROOF=REQUIRED
ACTOR_LABEL_ALONE_IS_REAL_HOST_PROOF=NO
```

The repository integration test may use ordinary Python subprocesses to prove independent
PIDs, independent connections, distinct effective `HOME`, distinct gateway instances,
and one shared write barrier. That result is process-boundary evidence only. A real-host
claim additionally requires accepted provenance that one actor command was launched from
the actual Codex execution context and the other from the actual Hermes execution
context using the same candidate bytes and shared disposable run directory.

### 4C-04 tracked harness boundary

The tracked harness is `tools/s6b4c04_disposable_shared_store.py`. It provides exactly
three mechanical phases:

1. `prepare` creates a fresh disposable database and seeds one current revision;
2. each real execution context runs its own `actor` command against that same run;
3. `verify` reads the resulting disposable state and emits the bounded verdict.

The actor path constructs the existing `SQLiteVecEngine`, `MemoryAdapter`, `HostBinding`,
and `MemoryGateway` independently in each process. A file-backed test barrier is installed
only at the adapter-to-storage write seam so both actors have completed the same current
revision pre-read before either authoritative write proceeds. The authoritative CAS remains
the accepted 4C-03 database transaction; the harness must not add a coordinator, lock,
retry loop, or alternate write path.

The harness uses an acceptance-only deterministic embedding provider so 4C-04 varies the
execution-context/process dimension without simultaneously varying embedding runtime
behavior. Async embedding contention and exact provider/backend runtime proof remain
4C-05 work. This provider substitution is proof instrumentation only and is not a
production profile or product behavior change.

Established candidate diagnostics confirm `CODEX_SANDBOX_INTERACTION=CONFIRMED`: normal
Codex sandbox execution could access the shared evidence path but stalled at
`OPEN_ADAPTER_START`, while the same candidate and Python 3.11 environment reached
`BARRIER_READY` from the Owner shell and from an actual Codex context launched with
`codex --sandbox danger-full-access`. `PYMILVUS_CAUSE=REJECTED`,
`PYTHON_3_14_RUNTIME_DRIFT=REJECTED`, and `THREAD_AFFINITY_CAUSE=REJECTED`; the failure
boundary is Codex sandbox interaction, not FactLane CAS/storage behavior.

For final real-host acceptance only, use the already-established Python 3.11 environment
from the same locked dependencies as canonical CI and start the Codex actor from an
actual `codex --sandbox danger-full-access` session. This is an acceptance reproducibility
choice and acceptance-only execution profile, not a product Python-support restriction or
production requirement. The runbook's separate process-local HOME paths and its
process-local `MCP_MEMORY_BASE_DIR=<RUN_DIR>/upstream-runtime/<actor>` are acceptance
isolation only; actual Codex/Hermes launch provenance remains external accepted evidence.

### 4C-04 required invariants

The process proof and real-host proof must satisfy:

```text
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

The process-level proof additionally requires distinct PIDs, effective homes, gateway
instance IDs, and explicit disposable host bindings. The real-host acceptance additionally
requires external accepted launch provenance for actual Codex and Hermes contexts; the
verifier must not infer that provenance from actor labels, `HOME`, hostname, cwd,
credentials, or PID.

Evidence is disposable and remains only under:

```text
.factlane-local/evidence/s6b4c-04/<fresh-run-id>/
```

The database, manifest, barrier files, result files, and raw command output must remain
ignored/untracked. No live Codex/Hermes configuration, credentials, native memory, global
MCP registration, production store, backend pin, schema, embedding model, or `uv.lock`
may change.

### 4C-04 closure and verification boundary

A CI subprocess PASS alone is insufficient to close 4C-04. Closure requires all of:

1. deterministic RED-to-GREEN harness coverage in repository history;
2. process-boundary integration proof on the exact candidate code;
3. actual Codex and actual Hermes launch provenance using that same candidate;
4. `verify` PASS with one winner, one `VERSION_CONFLICT`, no lineage fork, and no partial loser state;
5. exact final-head repository verification;
6. normal PR and Owner/Advisor merge disposition.

The minimum repository verification remains:

```text
uv sync --frozen --dev
uv run pytest -q
uv run python -c "import factlane; print(factlane.__name__)"
uv run factlane --help
```

The supporting mechanical commands are documented in
`docs/S6B_4C_04_DISPOSABLE_HOST_ACCEPTANCE_RUNBOOK.md`. That runbook is not parallel
execution authority. The 4C-04 result alone did not authorize 4C-05; the separate Owner
authorization and current 4C-05 contract follow below. Crash injection, live
configuration mutation, native-memory mutation, global registration, and production
migration remain outside this campaign boundary.

## 4C-05 contract: async embedding concurrency and pinned-backend runtime proof

4C-05 removes event-loop blocking caused by synchronous local embedding-provider I/O at
the async FactLane adapter boundary and proves the existing exact local provider plus
pinned SQLite-vec backend runtime under controlled concurrent embedding work. This
slice changes only the async scheduling boundary; it does not change the provider,
storage, schema, public API, or accepted 4C-03 CAS behavior.

### Required async boundary

Potentially blocking synchronous provider operations used from async adapter paths must
not execute on the event-loop thread:

- document embedding (`embed_documents`), including `store` and `update`;
- query embedding (`embed_query`), including semantic and hybrid `search`;
- provider status/readiness HTTP probing (`provider_status`), including adapter creation
  and `status`.

The synchronous `EmbeddingProvider` protocol remains synchronous. The intended smallest
implementation candidate is adapter-side `asyncio.to_thread(...)` using the standard
asyncio default executor around those provider calls. RED/GREEN evidence must establish
whether that candidate is sufficient before any broader design is considered.

Provider worker threads perform provider work only. They must not own, pass, or
manipulate SQLite connections, and the storage executor path is unchanged.

4C-05 must not introduce:

- an embedding coordinator, worker service, or new queue;
- a custom retry layer or new backpressure/configuration subsystem;
- `aiohttp` or another network dependency;
- an async rewrite of `OllamaLocalProvider`;
- backend thread-affinity changes or storage executor changes.

### Preserved provider contract

The async boundary must preserve all existing provider behavior:

- local-only HTTP and no remote fallback;
- exact model digest validation;
- document/query prefixes;
- `truncate=false`;
- requested output dimension;
- normalized finite-vector validation;
- existing stable `AdapterError` codes.

No production embedding profile is selected in 4C-05. For later real-runtime
acceptance, `nomic-256` may be used only as the already-recorded engineering pilot
candidate:

```text
PRODUCTION_EMBEDDING_PROFILE_SELECTION=NO
```

### Preserved storage/write contract

- The accepted 4C-03 transaction-local CAS remains authoritative.
- The existing adapter-local write lock is not promoted to correctness authority and is
  not redesigned here.
- The pinned backend retains WAL, busy-timeout, and retry ownership.
- No schema, backend pin, lockfile, public tool, or gateway change is permitted.
- Provider worker threads never own or manipulate SQLite connections.

### Cancellation and crash boundary

4C-05 does not solve process kill, cancellation cleanup, crash injection, or interrupted
worker-thread semantics. Those belong to 4C-06 unless a concrete correctness defect
prevents the 4C-05 proof. No crash-injection or cancellation campaign is part of this
slice.

### Deterministic RED proof

Before production changes, add focused integration coverage using a deterministic
synchronous test provider with valid normalized vectors, a `threading.Barrier` with a
bounded timeout, thread IDs, and active-call tracking. The tests require no Ollama or
network access and must fail deterministically against the current direct synchronous
provider calls.

Required future invariants are:

```text
EVENT_LOOP_BLOCKED_BY_QUERY_EMBEDDING=NO
CONCURRENT_QUERY_EMBEDDING_OVERLAP=YES
EVENT_LOOP_PROGRESS_DURING_EMBEDDING=YES
EVENT_LOOP_BLOCKED_BY_DOCUMENT_EMBEDDING=NO
EVENT_LOOP_BLOCKED_BY_PROVIDER_STATUS=NO
```

The document-embedding test uses genuinely independent adapter instances when checking
overlap, so it measures provider/event-loop behavior without defeating the existing
adapter-local write lock. It must not duplicate the 4C-03/4C-04 CAS campaign or add a
new storage coordination mechanism.

### Pinned-backend runtime proof

After the RED/GREEN cycle, the runtime proof must exercise the existing
`OllamaLocalProvider` local-only HTTP boundary and the exact pinned SQLite-vec backend
under the same locked dependency contract used by canonical CI. Deterministic CI
coverage proves scheduling behavior without external services; it does not by itself
claim exact local-provider runtime acceptance. No `pymilvus` addition, backend pin
change, embedding model change, or production profile selection is authorized.

The 4C-05 current phase is design and RED only. A passing RED test is not a closure
result, and no GREEN production fix or 4C-06 work is authorized by this contract.
