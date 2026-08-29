# S6B.4C Shared-Store Concurrency Specification

## Status

```text
SPEC_STATUS=OWNER_AUTHORIZED_IN_PROGRESS
OWNER_AUTHORIZATION_DATE=2026-08-29
CURRENT_SLICE=S6B_4C_02_TRANSPORT_BOUND_HOST_IDENTITY_AND_SHARED_GATEWAY
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

4C-01 is CLOSED_PASS. The current implementation slice is 4C-02 only. 4C-02 must not implement atomic multi-client CAS, lost-update prevention, concurrent writers, crash injection, or embedding concurrency; those remain later campaign slices.

```text
4C-01=CLOSED_PASS
4C-02=CURRENT
4C-03=NOT_STARTED
4C-04=NOT_STARTED
4C-05=NOT_STARTED
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
