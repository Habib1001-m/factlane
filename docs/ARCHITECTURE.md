# Architecture

FactLane is a fact-sharing service for MCP agents. Its stored records support a decision but
do not supersede the user's current instructions, repository state, or a live authoritative
source. The system keeps the transport, authorization, fact lifecycle, and storage
boundaries separate.

## Request path

```text
Local MCP host / trusted launcher
  -> immutable host and write-context bindings
  -> stdio MCP gateway (five tools)
  -> scope, identity and authority checks
  -> memory adapter / bounded retrieval routing
  -> local embedding provider + SQLite-vec storage
```

`MemoryGateway` receives the five operations: `memory_search`, `memory_get`,
`memory_store`, `memory_update`, and `memory_status`. It rejects unsupported transports
and caller-supplied host identity claims. `MemoryAdapter` owns request validation,
freshness, permissions, contradiction handling, retrieval budgets, and revision policy;
`TruthRouter` selects bounded search behavior inside the adapter. The backend supplies
SQLite connections and SQLite-vec primitives, not FactLane's authorization decisions.

The public server boundary converts governed `AdapterError` failures into the ordinary MCP
result shape with `status=BLOCKED`, stable `error_code`, safe `message`, empty `results`, and
trusted audit metadata. This conversion exists only at the public server boundary; direct
gateway/adapter callers still receive `AdapterError`, and unexpected exceptions remain MCP
transport errors rather than being relabeled as governed outcomes.

The server supports **local stdio only**. Codex and Hermes are tested host integrations, but
the dispatch path is not specific to either. Another client that supports command-launched
stdio MCP may use the same executable; it is not automatically a separately qualified host.
Launcher identity is not operating-system or cryptographic attestation.

## Scope and trusted identity

The public contract defines five scopes:

| Scope | Identity requirement |
| --- | --- |
| `GLOBAL_USER` | No project/workflow identity. |
| `PROJECT` | An exact `project_id`. |
| `WORKFLOW` | Exact `project_id` and `workflow_id`. |
| `TOOL_ENVIRONMENT` | An exact `agent_id`. |
| `CROSS_PROJECT_WORKFLOW` | **All** project, worktree, workflow, and agent identity keys must be absent. |

When the trusted launcher supplies current-context identities, the gateway inserts
applicable missing values for `PROJECT`, `WORKFLOW`, and `TOOL_ENVIRONMENT`. A
conflicting explicit value fails with `BOUND_CONTEXT_IDENTITY_MISMATCH`; the original
request is not rewritten. `GLOBAL_USER` and `CROSS_PROJECT_WORKFLOW` do not inherit bound
identities. The latter scope is for workflow rules that apply across projects, not a query
that searches every project in turn.

## Contributions, verification, and revisions

A normal process starts with `--write-profile read-only` unless the launcher grants another
profile. `delegated-candidate` permits an agent to submit a bounded `CANDIDATE` with source
provenance, a freshness policy, and an idempotency key. It does not permit that agent to
mint `VALIDATED_CURRENT` by asserting a verifier identity in its request.

That runtime grant is separate from content consent. Candidate content is eligible for submission
only after an explicit user remember/store request or an agent proposal that the user explicitly
authorizes. Content consent does not change the launcher profile or grant verifier authority.

Promotion is a distinct `memory_update` operation: a trusted verifier uses `REVERIFY`, the
expected revision, and the exact Candidate `expected_record_id`. Storage contract v2 keeps
`contribution_origin` separate from verification and preserves it on promotion. The verifier
is recorded separately; an older record is not silently rewritten as if it had been
contributed by the verifier. `REVERIFY` also preserves contradiction identity: the fact,
scope, memory type, and subject remain the same logical memory. Identity-bearing assertions
must match the existing record; semantic reclassification uses `REPLACE`.

Revision changes use transaction-local compare-and-swap. A stale independent writer receives
`VERSION_CONFLICT`. For a Candidate promotion, exact scope, logical memory, parent record,
revision, lifecycle, and contradiction checks occur inside the same SQLite transaction.
Duplicate or retried requests are resolved through the governed idempotency rules; an
idempotency key from a more privileged request is not an authorization token.

Storage contract v2 also blocks stale legacy writers from inserting, updating, or deleting
adapter records through an untrusted raw SQLite connection. The trusted maintenance path has
a separate authorization boundary; it does not make arbitrary direct SQL writes part of the
public API. Because writer authorization is a connection-local SQLite function, an unregistered
raw connection against a compatibility-bound Development database is rejected at function
resolution for `factlane_contract_v2_compat_writer`. When opening an exact legacy v0.1.3 database,
the current runtime resolves the older `factlane_contract_v2_writer` as **deny** until compatibility
is proven and the persistent fences are replaced atomically. Only the separately authorized
maintenance/recovery path can enable that legacy writer capability for its bounded operation.

## Retrieval and history

`CURRENT` retrieval admits only eligible `VALIDATED_CURRENT` facts within the exact scope.
For keyword search, the lifecycle filter precedes SQL limits. For semantic search,
sqlite-vec KNN constrains eligible row IDs **inside the vector query before `k` **.
Otherwise, many closer unverified Candidates could fill the top results and conceal a
farther validated fact.

After CURRENT semantic/hybrid candidates pass lifecycle and freshness checks, the adapter
selects the highest-ranked candidate from distinct stored provenance sources before filling
remaining slots with repeated-source candidates. This reduces same-document crowding without
changing stored scores: the top eligible result is preserved, and the selected set is emitted
in its original relevance order. After the top result, an unseen source can therefore occupy a
slot ahead of a higher-scored repeat from the same source; this is the deliberate diversity
tradeoff rather than a score rewrite. Exact, keyword-only, and `REVIEW_HISTORY` retrieval
bypass this diversity step.

`REVIEW_HISTORY` exposes past revisions and Candidates for explicit inspection. Atomic
compaction can retain a historical record without its original vector. If semantic or hybrid
history is consequently incomplete, the result reports
`degradation=HISTORY_SEMANTIC_PARTIAL`. When output also exceeds its result budget,
`budget.truncated=true` preserves the independent truncation signal; an ordinary budget-only
truncation reports `BUDGET_EXCEEDED`.

## Storage and runtime compatibility

FactLane uses a pinned `mcp-memory-service` backend for reusable SQLite/SQLite-vec
mechanics, connection locking, bounded busy retries, WAL initialization, and
connection-related primitives. FactLane owns higher-level schema/transaction and authority
semantics. It does not assume a Python version proves SQLite feature support.

The linked SQLite runtime must be **3.42.0+**. `SQLiteVecEngine.open()` rejects an older
runtime before database creation or opening, with `BACKEND_COMPATIBILITY_MISMATCH`. The
floor covers sqlite-vec's eligible-row `IN` behavior and the FTS5 `secure-delete` feature
required by sensitive-memory recovery. That operator also performs an independent feature
probe before mutation.

Embedding calls use an `EmbeddingProvider` contract. The currently shipped provider is
Ollama over loopback HTTP; provider version, model family, capability, dimension, context, and
input-size checks fail closed. Potentially blocking provider calls are offloaded from the asyncio event loop. No
cloud embedding provider or automatic external fallback is shipped. See the
[environment policy](ENVIRONMENT.md) for built-in profiles and exact prerequisites.

The `v0.1.4` compatibility layer treats vector-space identity separately
from runtime provenance. Semantic identity binds model identity/family, source and output dimensions,
document/query prefixes, normalization, distance metric, projection revision, and FactLane's
embedding-compatibility revision. Observed Ollama version and model digest remain runtime provenance;
they are recorded on the binding and the per-record digest is preserved rather than retroactively
rewritten. A runtime change may therefore be accepted only when the semantic identity still matches
and a qualified, integrity-bound cross-space anchor proves direct old-vector/new-vector compatibility.

For the exact known v0.1.3 `embeddinggemma-300m-768` legacy profile, a successful proof performs a
metadata-only migration: existing vectors and adapter-record embedding provenance remain byte-for-byte
and value-for-value unchanged, while revisioned compatibility metadata and the new stale-writer fence
are committed atomically. Missing, unknown, mixed, or incompatible legacy identity remains
`UNPROVEN`/`INCOMPATIBLE`: `memory_get`, `EXACT`, `KEYWORD`, and read-only status remain available,
but `SEMANTIC`, `HYBRID`, writes, and vector-mutating maintenance fail closed. FactLane does not apply
an automatic rotation/projection repair and does not automatically re-embed durable facts; re-embedding
is a separate explicit maintenance/migration boundary.

## Maintenance and incident recovery

`memory_status` provides bounded, read-only capacity and retention observations. Manual
housekeeping compacts eligible superseded material through an atomic path while preserving
current authority and logical history. It is not an automatic retention daemon, full backup
facility, or sensitive-content erase operation.

Sensitive-memory incident recovery lives in a **trusted local operator**, outside
`MemoryGateway` and the five MCP tools. It requires explicit authorization, exact
target/database binding, maintenance quiescence, known schema and propagation, supported
SQLite and verified FTS5 capabilities. A pre-commit failure rolls back; post-commit sealing
failure leaves a durable database-resident recovery interlock that blocks ordinary runtime
startup across process restarts until verified recovery completes. Local purge does not prove
erasure from external copies or physical media. See [Security](../SECURITY.md).

Runtime/recovery exclusion is cooperative and process-independent on supported POSIX hosts:
ordinary `SQLiteVecEngine` instances resolve their database path once, acquire a shared advisory
`flock` on that database inode before backend initialization, and retain it until backend closure
is proven. Recovery acquires the corresponding exclusive inode lock before its first quiescence
check. During sealing it also locks the sanitized replacement inode **before** `os.replace()`,
then retains both the retired old-inode lock and the promoted new-inode lock through the
operator-owned postflight. Hard-link aliases therefore converge on the same kernel lock identity,
and promotion never exposes an unlocked replacement inode. A new runtime attach during recovery
receives `MAINTENANCE_IN_PROGRESS`. The operator's own postflight engine uses an internal,
exact-inode capability derived from the still-live exclusive lease.

If postflight cleanup cannot prove its SQLite handle closed, including task cancellation, recovery
keeps all process-owned exclusive inode leases retained fail-closed instead of reopening normal
service. Process exit releases those descriptors; the sealing-incomplete state still requires
operator reconciliation.

The exclusion guarantee is bounded to supported local POSIX filesystems with reliable `flock`
semantics; sensitive-memory recovery maintenance remains unsupported on Windows and on unvalidated
network/FUSE locking behavior. The `v0.1.4` standard runtime is qualified
separately on native Windows x64/AMD64 for normal import/startup, stdio MCP, SQLite WAL/busy/CAS,
process concurrency, durability, and valid Windows paths. That Windows qualification does not add a
Windows recovery implementation: recovery imports safely but fails closed before mutation when the
POSIX `fcntl`/`flock` capability is unavailable. The recovery exclusion boundary is cooperative,
not a defense against arbitrary raw filesystem replacement by a privileged external process, so the
independent quiescence inventory/full-stop procedure is still part of recovery.

## Version and migration boundary

The Python package/runtime and the durable database are related but distinct compatibility
surfaces. Installing a newer package does not itself migrate a database, and successful package
replacement does not prove that an older runtime can safely reopen state touched by a newer
release. A release that changes stored schema or data semantics must provide an explicit
migration contract, post-migration verification, and any supported rollback/restore path.

`v0.1.3` is the first official production release and does not define a cross-version
production-data downgrade contract. The release-operations runbook defines a published release
by its recorded tag target, Git tree, and artifact digests, and requires later maintenance not to
silently retarget that tag or replace its assets to match different bytes. Operators still verify
those identities because the hosting platform can technically permit mutation. Later
documentation or development changes on `main` do not change the recorded identity of an already
published release. The exact release-identity and transition procedure lives in
[Release operations](RELEASE_OPERATIONS.md).

## Qualification boundary

The `v0.1.4` qualification includes native Windows x64/AMD64 standard
runtime behavior: package import, command startup, stdio discovery of exactly five public tools,
default read-only authority, Candidate-only delegated contribution, verifier promotion, SQLite
3.42.0+ with sqlite-vec, WAL/busy handling, cross-process CAS/concurrency, fresh-process durability,
and valid Windows path handling. The pinned sqlite-vec dependency is qualified here on x64/AMD64;
this is not a Windows ARM64 claim. Sensitive-memory recovery maintenance remains the POSIX-only
capability described above.

FactLane 0.1.4 is production-qualified for the documented local configuration: the packaged
Python runtime, linked SQLite/SQLite-vec storage contract, stdio MCP surface, supported local
embedding profile, and configured local host integrations. The qualification exercised
backup/restore compatibility, bounded concurrent operation, crash/restart rollback,
production-derived retrieval, and fail-closed SQLite capacity behavior.

That evidence does not establish support for every MCP client, arbitrary filesystem,
production-scale ingestion pattern, unlimited-duration load, or every language mix.
Source-diverse CURRENT semantic/hybrid selection reduces one known document-crowding
mechanism, but semantic relevance — including Arabic/mixed-language ranking — remains
workload-specific. FactLane is a fact store, not a transcript archive or raw document
crawler.
