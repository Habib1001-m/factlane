# Security policy

FactLane stores durable facts that may outlive an agent session. Its security boundary is
designed to keep unverified contributions separate from current facts and prevent callers
from granting themselves access or write authority.

## Reporting a vulnerability

Report security issues **privately to the repository owner**. Do not post credentials, raw
memories, transcripts, private file paths, or evidence bundles in a public issue. Supply the
smallest synthetic reproduction that demonstrates the behavior, together with the affected
release/commit and relevant runtime versions.

## Trust boundaries

- **Host identity:** A trusted launcher binds a stable host identity to the local `stdio`
  MCP gateway. Caller-supplied transport identity and conflicting host-bound scope
  identities are rejected. Launcher binding is not cryptographic process attestation; the
  host and its launch configuration are part of the trusted environment.
- **Write authority:** A connection is read-only unless the launcher explicitly selects a
  write profile. Delegated agents may contribute `CANDIDATE` records but cannot validate
  them by changing request fields or reusing a privileged idempotency key. Current
  verification requires an independently trusted launcher/operator context.
- **Scope isolation:** Reads and writes operate in an exact scope. `CROSS_PROJECT_WORKFLOW`
  accepts no project, worktree, workflow, or agent identity keys and does not implicitly
  copy records into another scope.
- **Storage consistency:** Revision updates use transaction-local compare-and-swap. The
  storage v2 contract rejects raw legacy writes to adapter records, and supported operations
  have atomic rollback and idempotent retry behavior at the documented transaction
  boundaries. An unregistered raw SQLite connection fails closed while resolving the
  connection-local `factlane_contract_v2_writer` authorization function; SQLite therefore
  reports `no such function: factlane_contract_v2_writer` rather than the trigger's internal
  `FACTLANE_STORAGE_V2_WRITER_REQUIRED` marker. That diagnostic difference does not grant a
  write path. SQLite capacity exhaustion reported as `SQLITE_FULL` is translated at the
  FactLane boundary to retryable `BACKEND_UNAVAILABLE`; the failed mutation remains
  transactional and does not leave a partial durable record.
- **Local providers:** The shipped embedding provider connects to Ollama on loopback;
  non-local embedding endpoints and automatic remote fallbacks are not supported. Embedding
  model identity, capabilities, dimensions, and input limits are checked against the
  selected profile.

The public MCP surface is limited to `memory_search`, `memory_get`, `memory_store`,
`memory_update`, and `memory_status`. There is no normal-agent delete, recovery,
administration, background harvesting, or autonomous consolidation tool.

## Sensitive-memory recovery

The local maintenance operator in `factlane.recovery` is separate from MCP and requires
explicit operator authorization. It is intended for a confirmed sensitive-memory incident,
not routine history maintenance.

Before modifying a database, recovery checks the exact target set and database/profile
binding, the supported schema and materialized state, the absence of competing database use,
and the required storage/FTS capabilities. An unsupported linked SQLite runtime (below
**3.42.0**) stops the operation before it acquires a maintenance lease or creates recovery
state or receipts. The operator separately verifies FTS5 `secure-delete`; the version
number alone is not considered sufficient.

Normal `SQLiteVecEngine` runtime attachments hold a shared advisory `flock` on the resolved
database **inode itself** for the lifetime of the engine. Sensitive-memory recovery takes an
exclusive lock on the current database inode before its first quiescence check. Before replacing
that database with the sanitized image, recovery also takes an exclusive lock on the sanitized
replacement inode; `os.replace()` then runs while both old and new database inodes remain locked.
The old-inode and promoted-inode locks are retained through real-engine postflight. This prevents
ordinary FactLane runtimes from splitting the exclusion through hard-link aliases or through a
pathname-derived sidecar lock. A normal runtime attachment attempted against either protected
inode fails closed with `MAINTENANCE_IN_PROGRESS`; recovery attempted while a normal runtime
engine is still attached fails before mutation.

If postflight database cleanup fails or is cancelled while a SQLite handle may remain live,
FactLane deliberately retains the process-owned exclusive inode locks fail-closed instead of
reopening normal service. Process exit closes those retained descriptors together with remaining
process-owned SQLite handles; the incident still requires operator reconciliation before service
is restarted.

After a sensitive logical purge commits, FactLane also records a recovery interlock inside the
database itself. Ordinary runtime startup checks that durable interlock before backend service and
returns `MAINTENANCE_IN_PROGRESS` while sealing/recovery remains incomplete. Because the marker is
stored in the database, it survives process exit/restart and follows the database inode through
hard-link aliases and the sanitized-image promotion path. Recovery maintenance access is the only
startup path allowed to bypass it, and successful verified recovery clears it.

This exclusion guarantee is scoped to supported local POSIX filesystems with reliable `flock`
semantics. It is not a claim about Windows or unvalidated NFS/SMB/FUSE locking behavior; live
recovery on such storage remains outside the accepted recovery contract.
It is also a cooperative FactLane runtime contract, not protection against an arbitrary privileged
process replacing database paths or bypassing FactLane with raw filesystem/database writes; the
existing quiescence inventory and full-stop incident procedure remain required for those cases.

A pre-commit failure rolls back the logical purge. If logical purge commits but subsequent
sealing fails, the database must not be reopened for normal service or restored from a copy
containing the sensitive payload until safe recovery completes. The operation does not
create a plaintext backup by default. Any forensic snapshot, handling of external copies, or
live production recovery requires its own authorization and containment plan.

**Local recovery is not a guarantee of physical erasure or removal from backups, snapshots,
logs, or external systems.** Normal `memory_update`, superseded-record compaction, and
manual housekeeping are not substitutes for incident recovery.

## Known limits

FactLane is a local service, not a distributed consensus system. Version 0.1.3 is
production-qualified for the documented local deployment profile, including authoritative
backup/restore compatibility, bounded concurrent operation, crash/restart rollback, configured
stdio host integration, and production-derived retrieval checks. Operators still need their
own backup retention, restore procedures, monitoring, and deployment-specific recovery plan.

The qualification does not establish universal behavior for arbitrary network/FUSE
filesystems, every MCP client, unlimited-duration load, or every language and ranking
workload. Deterministic SQLite capacity exhaustion and permission-read-only behavior were
exercised; a true kernel ENOSPC condition and a true EROFS mount are outside that evidence.
Arabic/mixed-language semantic specificity remains workload-specific. None of these
limitations permits bypassing scope, authority, freshness, or verification policy.

See the [architecture](docs/ARCHITECTURE.md) and
[environment requirements](docs/ENVIRONMENT.md) for implementation and runtime details.
