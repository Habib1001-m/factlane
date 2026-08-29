# S6B.4C Shared-Store Concurrency Specification

## Status

```text
SPEC_STATUS=OWNER_AUTHORIZED_IN_PROGRESS
OWNER_AUTHORIZATION_DATE=2026-08-29
CURRENT_SLICE=S6B_4C_01_ENTRY_PREFLIGHT_AND_HARNESS_FOUNDATION
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

The current implementation slice is 4C-01 only. In particular, 4C-01 must not implement concurrent writes, shared gateway transport, multi-client CAS, crash injection, or embedding concurrency.

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
