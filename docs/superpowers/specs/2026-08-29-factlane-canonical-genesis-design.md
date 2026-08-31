# FactLane Canonical Public Genesis — Design

**Original drafting status:** Owner-approved design amended after independent review; awaiting final written-spec approval
**Date:** 2026-08-29
**Source pilot head:** `6ac7f2a4a19c57e4e0e70c85dd055b5de9ad074c`
**Source branch:** `codex-s6b4b-memory-adapter`
**Public target:** `Habib1001-m/factlane`

```text
ARTIFACT_STATUS=HISTORICAL_IMPLEMENTED_GENESIS_DESIGN
CURRENT_EXECUTION_AUTHORITY=NO
```

This document records genesis design provenance. Current truth is `TASKBOARD.md`;
subsequent S6B.4C work is intentionally outside this genesis document.

## 0. Independent-review reconciliation

An independent multi-disciplinary review was performed against this design and
S6B Taskboard V13 only. The reviewer intentionally did not receive the runtime
repository, S6B.4B implementation reports, or prior test evidence. Findings were
therefore re-verified against the accepted pilot implementation before changing
this design.

Accepted reconciliation:

```text
NOMIC_TASK_PREFIX_RUNTIME=PASS_IN_S6B_4B
NOMIC_PREFIX_REGRESSION_TEST=REQUIRED_IN_GENESIS
NOMIC_EFFECTIVE_CONTEXT_AND_TRUNCATION_POLICY=GENESIS_HARDENING
S6B_4C_SEQUENCE=SUPERSEDED_BY_FACTLANE_CANONICAL_GENESIS
SMALL_ROW_STORAGE_NUMBERS=NOT_CAPACITY_PROJECTION
VEC0_RECLAIM_BEHAVIOR=INPUT_TO_S6B_4D
REVIEW_ARCHIVE_CUSTODY=PRIVATE_FORENSIC_EVIDENCE
LICENSE_VERIFICATION=EXACT_PINNED_REVISIONS_ONLY
RECURRING_FORMATTER_GATE=NOT_ADDED_AT_GENESIS
UPSTREAM_SQLITE_CONCURRENCY_PRIMITIVES=REUSE_BEFORE_REIMPLEMENTATION
```

No review finding invalidated the accepted S6B.4B contract pilot. The review did
identify documentation, regression-test, provenance, and future-gate hardening
that this amended design now makes explicit.

## 1. Purpose

FactLane is a local-first, multi-host governed memory plane for AI agents. Its job is to share validated facts across eligible hosts without sharing whole context windows, raw transcripts, hidden reasoning, or execution authority.

The canonical product principle is:

```text
SHARE FACTS, NOT CONTEXT.
```

The first supported hosts are Codex and Hermes. Host integration lives at the edge; scope, freshness, authority, contradiction handling, lineage, retrieval budgets, and storage policy remain portable core behavior.

## 2. Canonical-genesis decision

The existing three-commit `one-linux-codex-memory` history is certified pilot provenance, not the desired public product history.

The public `factlane` repository will start from a clean canonical genesis commit. The old source head remains traceable through the review archive, manifest, and `docs/PROJECT_HISTORY.md`.

The genesis must not silently rewrite what was proven in S6B.4B. It carries forward only accepted source behavior plus explicit repository-governance improvements.

## 3. Product naming

Canonical public names:

```text
Product: FactLane
Python package: factlane
CLI: factlane
MCP server identity: factlane
Decision component: FactLane Router
Future shared multi-client component: FactLane Gateway
```

Internal descriptive class names such as `TruthRouter` may remain when they are technically precise. Branding alone is not a reason to rename stable internal symbols.

All public package metadata, import paths, entry points, documentation, examples, and repository references must stop using `one-linux-codex-memory` or Codex-specific product naming.

## 4. Repository shape

Canonical minimal structure:

```text
factlane/
├── AGENTS.md
├── TASKBOARD.md
├── README.md
├── LICENSE
├── SECURITY.md
├── pyproject.toml
├── uv.lock
├── environment-provenance.json
├── docs/
│   ├── ARCHITECTURE.md
│   ├── GOVERNANCE.md
│   ├── ENVIRONMENT.md
│   └── PROJECT_HISTORY.md
├── src/
│   └── factlane/
│       ├── __init__.py
│       ├── contract.py
│       ├── router.py
│       ├── adapter.py
│       ├── embeddings.py
│       ├── storage.py
│       └── server.py
├── tests/
│   ├── unit/
│   └── integration/
└── .github/
    └── workflows/
        └── ci.yml
```

`pilot.py` is not production runtime. Its reusable assertions and fixtures move to tests or development tooling. Generated `*.egg-info` is removed from source and ignored.

## 5. Public README contract

The README must explain FactLane without implying production readiness beyond accepted evidence.

It will state:

- local-first and CPU-capable baseline;
- five normal agent memory tools;
- current backend pin and why it is behind a narrow boundary;
- local embedding provider boundary;
- supporting-state, not execution-authority, semantics;
- current development status: S6B.4B accepted, S6B.4C not yet accepted;
- quick development setup using `uv`;
- no requirement for Docker, GPU, cloud LLM, or external embedding API in the baseline.

It must not market planned Codex/Hermes shared-store concurrency as already production-proven.

## 6. Agent bootstrap and canonical truth

### `AGENTS.md`

`AGENTS.md` is the repository entry point for coding agents. It is intentionally short and contains:

1. product identity and purpose;
2. authority order;
3. mandatory read order;
4. current task source (`TASKBOARD.md`);
5. environment policy;
6. Git/worktree discipline;
7. testing/verification expectations;
8. explicit stop/approval boundaries.

It must not become a historical project log or duplicate architecture documentation.

### `TASKBOARD.md`

`TASKBOARD.md` becomes the canonical live project board after genesis. External S6B V13 continuity material is a transition input only.

Rules:

- update the same board whenever a cycle changes project truth, debt, closure, or next action;
- keep accepted historical closures concise;
- every active item has status, evidence/commit reference where applicable, and next gate;
- do not create a new numbered taskboard file per cycle;
- implementation authority still comes from the active approved task/packet, not from stale summaries.

Initial canonical frontier:

```text
S6B_4B=CLOSED_PASS
REPOSITORY_CANONICALIZATION_AND_GOVERNANCE=ACTIVE_UNTIL_GENESIS_ACCEPTED
S6B_4C=PENDING_AFTER_GENESIS
S6B_4D=BLOCKED
S6B_5=BLOCKED
S6C_STARTED=NO
```

## 7. Governance

`docs/GOVERNANCE.md` defines the lightweight development loop:

```text
repo truth
→ TASKBOARD.md
→ task branch
→ implementation
→ tests/evidence
→ review
→ commit
→ taskboard/docs reconciliation
→ merge
```

Mandatory principles:

```text
REUSE_FIRST
VERIFY_BEFORE_REUSE
PROVENANCE_ALWAYS
REUSE_THE_ASSET_DO_NOT_INHERIT_THE_OWNER
CACHE_CAN_SEED_BUT_MUST_NOT_BECOME_HIDDEN_RUNTIME_AUTHORITY
AFTER_BOOTSTRAP_NO_UNBOUNDED_TOOL_HUNTING
```

No unrelated cleaning merely to obtain a clean baseline. Useless historical/noise artifacts may be removed or archived when the disposition is obvious and non-destructive, but valuable or ambiguous evidence is preserved until explicitly adjudicated.

## 8. Environment and provenance

`uv.lock` owns the resolved Python dependency graph.

`environment-provenance.json` records only important external runtime/build assets not sufficiently represented by the lock, for example:

- Python runtime requirement and resolved interpreter used for accepted verification;
- `uv` identity;
- Ollama runtime identity;
- approved Nomic and MiniLM model digests;
- effective embedding context-window capability used by each approved profile;
- embedding truncation policy and the verification method proving whether overflow fails closed;
- tokenizer measurement profile/artifact;
- external evidence tools only if an accepted workflow depends on them.

Each record includes a portable requirement, current resolved source, runtime owner, role, version/digest, verification method, and deterministic reacquisition method.

FactLane must not depend on Hermes Python, Hermes site-packages, Hermes configuration, or Hermes-owned caches as runtime authority. Hermes may operate the environment; it is not a product dependency.

### Embedding input and truncation policy

Nomic task semantics are already part of the accepted S6B.4B runtime contract:

```text
documents -> search_document:
queries   -> search_query:
```

Genesis adds durable regression tests so this behavior cannot silently drift.

Embedding input must fail closed on provider/model context overflow. FactLane
must not rely on silent provider truncation. The implementation must either:

1. use a provider option that rejects over-limit input; or
2. prove the effective model context bound at startup and reject over-limit input
   before the provider call.

Do not blindly force a nominal `num_ctx=8192`. The effective context capability
must be discovered/verified for the exact approved local model artifact and
recorded in `environment-provenance.json`. The current envelope caps facts at
2,000 UTF-8 bytes and search queries at 512 UTF-8 bytes; genesis must prove the
exact approved provider/model can accept those bounded inputs without silent
truncation, rather than inferring safety from the model's advertised native
context length.

## 9. Architecture boundary

The accepted core remains:

```text
Host edge
  → FactLane Router
  → five-operation narrow adapter
  → backend compatibility boundary
  → mcp-memory-service / SQLite-vec
  → local embedding provider
```

The five normal tools remain:

```text
memory_search
memory_get
memory_store
memory_update
memory_status
```

No normal agent surface exposes backend admin/delete/harvest/distill/consolidation/configuration operations.

### Backend compatibility audit

Before S6B.4C expands concurrency, the repository must document and test exactly which storage behavior FactLane owns versus the pinned backend.

Custom SQL or private backend hooks are accepted only when required to implement FactLane's scope, lineage, idempotency, authority, contradiction, or embedding-profile contract and when their compatibility assumptions are explicit.

Unnecessary reimplementation of backend ranking, storage, FTS, vector-search,
migration, or lifecycle primitives must be removed or delegated.

At the exact pinned backend commit, the compatibility audit must prefer existing
SQLite concurrency primitives before adding new infrastructure. Verified upstream
primitives include a connection lock, thread offload for synchronous DB work,
retry/backoff on SQLite `locked`/`busy`, WAL journal mode, and `busy_timeout`.
These primitives own SQLite access coordination and lock-contention handling;
they do **not** by themselves satisfy FactLane's contract-level revision/CAS,
idempotency, or lost-update semantics.

`sqlite-vec` vector deletion/reclaim behavior is a known lifecycle concern for
update/supersession-heavy workloads. Genesis records the concern; S6B.4D owns
the retention/compaction design and must verify the exact pinned behavior before
closing hygiene/recovery.

## 10. Pre-S6B.4C technical gates

Canonicalization does not declare S6B.4C ready by itself. Before multi-host concurrency work proceeds, the taskboard must explicitly track these debts:

```text
P0 HOST_IDENTITY_BINDING
P0 MULTI_CLIENT_WRITE_COORDINATION
P1 ASYNC_EMBEDDING_CONCURRENCY
P1 BACKEND_COMPATIBILITY_BOUNDARY
```

Interpretation:

- caller identity must come from the host/transport boundary, not arbitrary request payload;
- a shared multi-client write plane must not rely only on one process-local `asyncio.Lock`;
- synchronous embedding calls must not block the event loop under intended concurrent use;
- storage coupling to the pinned backend must be explicit and regression-tested;
- reuse the pinned backend's verified `_conn_lock`, thread-offload, locked/busy retry, WAL, and `busy_timeout` mechanics rather than duplicating them;
- FactLane still owns revision/CAS, idempotency, and lost-update semantics above those SQLite primitives.

## 11. Testing strategy

The S6B.4B pilot proved core behavior, but durable invariants move into repository tests.

Minimum canonical test layers:

### Unit

- exact scope validation;
- freshness independent of access time;
- router memory-skip behavior;
- exactly five normal tools;
- local-only provider URL guard;
- embedding profile invariants;
- Nomic task-prefix invariants (`search_document:` for stored text, `search_query:` for query text), including exactly-once prefix application;
- embedding context/truncation fail-closed behavior using the exact provider contract;
- provenance/fact security bounds;
- lifecycle and authority filtering;
- contradiction and supersession rules.

### Integration

- SQLite-vec open/read/write through the FactLane boundary;
- read-after-write and lineage;
- exact idempotency;
- stale/superseded exclusion from current retrieval;
- backend compatibility assumptions, including reuse of pinned upstream SQLite locking/retry/WAL primitives;
- revision/CAS and lost-update behavior as FactLane-owned semantics where applicable;
- provider calls using local test doubles by default.

Live Ollama/model benchmarks remain opt-in acceptance/evidence tests, not required for every CI run.

## 12. CI baseline

One simple workflow only:

```text
Python 3.11
→ install uv
→ uv sync --frozen --dev
→ uv run pytest
→ import/package/CLI sanity
```

Formatting/static sanity remains a one-time genesis construction check unless an existing locked tool already provides clear recurring value. Genesis does not add a formatter/linter dependency solely to create a CI checkbox.

No release automation, dependency-update bots, persistent services, Docker matrix, GPU matrix, or multi-host acceptance automation in the genesis slice.

## 13. Security and public-repository hygiene

`SECURITY.md` explains responsible reporting and the main product security boundaries.

Before public push:

- no secrets, auth material, raw user memory, transcripts, private local paths used as product constants, or private evidence bundles;
- no generated `.egg-info`, caches, databases, pilot evidence, or local virtual environments;
- no live host configuration;
- no historical Windows/Hermes/Shadow-Wolf memory content;
- no silent remote embedding fallback;
- public docs may mention current-machine evidence only when necessary and non-sensitive.

## 14. License and upstream attribution

FactLane uses Apache License 2.0.

The repository must preserve accurate attribution for the pinned `doobidoo/mcp-memory-service` dependency and must not imply ownership of that upstream project.

License compatibility is verified against the exact pinned dependency revisions, not the upstream repositories' current default branches. For the accepted S6B.4B baseline, `doobidoo/mcp-memory-service` is checked at commit `e5155b937051db4fa99a384018c5ebd621d8c5ef`; the pinned `sqlite-vec` dependency is checked at its resolved release/tag. FactLane does not vendor either source tree in this genesis slice.

## 15. Project history

`docs/PROJECT_HISTORY.md` records the transition without importing noisy evidence:

```text
Pilot repository: one-linux-codex-memory
Accepted source head: 6ac7f2a4a19c57e4e0e70c85dd055b5de9ad074c
Canonical review archive SHA-256: b8b61e1a1c1531baa0077eca9e5e1abf97b45bc828e34d9377d1250a6966089b
Canonical review archive custody owner: Project Owner
Canonical review archive class: PRIVATE_FORENSIC_EVIDENCE
Canonical review archive public distribution: NO
Canonical review archive integrity: SHA256 + git fsck
S6B.4B disposition: CLOSED_PASS
Public product identity: FactLane
```

The review archive itself is not committed to the public repository. The public history records filename/identity, custody class, owner role, SHA-256, and verification method; workstation-specific absolute custody paths remain private evidence rather than public product contract.

## 16. Clean-genesis construction

The public genesis is constructed from the accepted source tree, not by force-pushing the three pilot commits as product history.

Execution order after this spec is approved:

1. create an isolated genesis worktree/tree from the accepted source head;
2. rename package/project/CLI/server identity to FactLane;
3. remove generated pilot/build artifacts from tracked source;
4. move pilot-only logic out of runtime package while preserving reusable tests;
5. add governance/bootstrap/docs/provenance/security/license files;
6. add/strengthen durable tests for accepted invariants, including exactly-once Nomic prefixing and fail-closed embedding overflow; normalize the legacy Matryoshka pilot probe so it passes unprefixed fixture text through the provider instead of pre-prefixing a string that the provider prefixes again;
7. run formatting/static sanity only where already supported without adding tooling for its own sake;
8. run `uv sync --frozen --dev`, full pytest, package/import/CLI sanity, and Git hygiene checks;
9. reconcile `TASKBOARD.md` and docs to the verified result;
10. build a clean public tree with one canonical genesis commit;
11. push to the empty public `Habib1001-m/factlane` repository;
12. verify GitHub default branch/tree/CI state from the remote;
13. stop for Owner review before S6B.4C.

## 17. Non-goals for genesis

Do not implement in this slice:

- S6B.4C shared-store concurrency;
- live Codex or Hermes registration;
- real user memory bootstrap;
- housekeeping/retention engine;
- legacy salvage/import;
- Graphiti;
- Knowledge integration;
- persistent HTTP service;
- remote multi-machine transport;
- production embedding-profile cutover;
- release/package publication.

## 18. Acceptance criteria

The canonical genesis is acceptable only when:

```text
PRODUCT_NAME=FactLane
PUBLIC_REPO=Habib1001-m/factlane
PUBLIC_HISTORY=CLEAN_CANONICAL_GENESIS
SOURCE_PILOT_HEAD_TRACEABLE=YES
TRACKED_GENERATED_EGG_INFO=NO
PACKAGE_IMPORT=factlane
CLI=factlane
AGENTS_BOOTSTRAP=PASS
CANONICAL_TASKBOARD=PASS
ARCHITECTURE_DOC=PASS
GOVERNANCE_DOC=PASS
ENVIRONMENT_PROVENANCE=PASS
NOMIC_TASK_PREFIX_REGRESSION_TEST=PASS
EMBEDDING_OVERFLOW_FAILS_CLOSED=PASS
REVIEW_ARCHIVE_CUSTODY_RECORDED=YES
SECURITY_DOC=PASS
APACHE_2_LICENSE=PASS
BACKEND_PIN_PRESERVED=YES
EXACT_PIN_LICENSE_VERIFICATION=PASS
UPSTREAM_SQLITE_CONCURRENCY_REUSE_AUDITED=PASS
FIVE_TOOL_CONTRACT_PRESERVED=YES
TESTS=PASS
UV_FROZEN_SYNC=PASS
PUBLIC_SECRET_SCAN=PASS
LIVE_HOST_MUTATION=NONE
S6B_4C_STARTED=NO
```

Any semantic regression of accepted S6B.4B contract behavior blocks the public push.
