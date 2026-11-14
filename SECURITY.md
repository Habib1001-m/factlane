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
- **Storage consistency:** Revision and lifecycle updates use transaction-local compare-and-swap.
  Storage contract v3 keeps lifecycle-event history append-only and blocks ordinary mutation of
  lifecycle-operation rows. Sensitive-memory Recovery has a separate trusted writer path that may
  tombstone target-linked operation replay material while appending its own purge event. The storage
  contract also enforces contradiction-group CAS and a connection-local `factlane_contract_v3_writer` fence. Exact
  storage-v2 state is not opened for ordinary service; startup fails closed with
  `MIGRATION_REQUIRED` until the explicit migration operator is used. SQLite capacity exhaustion
  reported as `SQLITE_FULL` is translated at the FactLane boundary to retryable
  `BACKEND_UNAVAILABLE`; the failed mutation remains transactional and does not leave a partial
  durable record.
- **Local providers:** The shipped embedding provider connects to Ollama on loopback;
  non-local embedding endpoints and automatic remote fallbacks are not supported. Embedding
  model identity, capabilities, dimensions, and input limits are checked against the
  selected profile.

The public MCP surface is limited to `memory_search`, `memory_get`, `memory_store`,
`memory_update`, and `memory_status`. There is no normal-agent delete, recovery,
administration, background harvesting, or autonomous consolidation tool.

Direct Owner authority can use `REVOKE` and `REJECT` through `memory_update`; these modes do not add
another MCP tool. They require exact revision, exact current `expected_record_id`, contradiction-group
CAS, and a bounded reason code. The local `OwnerControlService` is a separate trusted operator boundary that also provides
bounded inventory/detail, conflict review and resolution, health/retention inspection,
reverification/replacement, manual freshness recheck, and readmission.
The packaged `factlane-owner` CLI binds scope and direct Owner authority at launcher creation and
accepts mutation proof as bounded JSON. The packaged `factlane-owner-webui` launcher is the direct
loopback WebUI path: it delegates to the same service and binds exactly to `127.0.0.1`. Startup
prints the bound origin plus a short-lived one-use bootstrap token. The client exchanges that secret only through the
`X-FactLane-Bootstrap` header on `POST /owner/bootstrap`; bootstrap credentials in the URL are
rejected, and the UI does not store the bootstrap token in `localStorage`. Successful exchange
consumes the token, sets an `HttpOnly; SameSite=Strict` session cookie, and returns the CSRF token
required by later mutation requests. It is an Owner control surface, not an HTTP MCP server and not
remote administration.

The managed Owner Panel is a separate platform-owned lifecycle driven by the main `factlane` CLI.
Its current primary navigation is **Overview**, **Memories**, **Review**, **Conflicts**, **Database**,
**Agents**, **Activity**, and **Settings**. Owner-wide Memories/Review discovery is authenticated,
read-only, and bounded to the selected local database; each result carries exact scope identity and
mutation still refreshes exact record/revision/Group-CAS proof. **Activity** exposes the governed,
filterable local operation/lifecycle history; local redacted Diagnostics live under **Settings**.
Neither surface is an external telemetry/upload channel. On Native Windows x64/AMD64, the managed path uses a
dedicated Firefox profile and local HTTPS with owner-private trust material rather than the direct
loopback bootstrap URL. It does not fall back to Edge/Chrome, install a system trust root, modify the
hosts file, or require certificate-bypass flags.

Managed enrollment uses one short-lived local request, pre-authentication binding, and a
fragment-only connection locator that is scrubbed from the page location after use. Reusing or
redelivering one locator to multiple browser documents is not a supported recovery mechanism; a
pre-authentication mismatch fails closed as `PENDING_CLAIM_CONFLICT` (HTTP 409) and can invalidate
the contested enrollment. Discard that enrollment and start a fresh `factlane open webui`; do not
retry or reuse its locator. A successful managed session uses a host-only
`Secure; HttpOnly; SameSite=Strict` cookie plus a separate CSRF
token. A valid session can resume across a page reload with CSRF rotation; resume does not reset the
30-minute inactivity limit or the 8-hour absolute session lifetime. Background polling is not user
activity.

Ordinary native control responses keep the 10-second control bound. Enrollment approval and
sensitive-action approval are explicit human-confirmation operations and allow up to 125 seconds for
the response. If that confirmation window expires, the surfaced control failure is
`CONTROL_UNAVAILABLE`; do not recover by replaying an old enrollment locator.

Browser mutations remain bound to the trusted launcher/platform scope, authenticated session, CSRF
token, exact record revision, idempotency, and the current contradiction-group CAS token where
required. The managed panel keeps record/revision/CAS identities internal to its ordinary decision
flow while it presents human-readable memory content to the Owner. Full database restore and permanent purge add a second gate: a fresh exact preview plus
native sensitive-action approval and a short-lived matching grant before commit. Restore also
requires explicit acknowledgement that active database state will be replaced; purge requires the
exact typed confirmation displayed by the preview. A recovery-required retry retains the exact
operation/preview identity but requires fresh sensitive approval rather than reusing a consumed
grant. Ordinary session access is not sufficient destructive-action authorization.

`on_change` freshness does not create an autonomous monitor. The Owner manual-recheck operation
accepts explicit bounded observation evidence and fails closed when evidence is stale, mismatched,
changed, unavailable, or indeterminate. A `REVOKED` memory can be readmitted only through the
governed readmission path with new verification and CAS. Readmission is a logical lifecycle action;
it is not filesystem/database restore and cannot recreate data removed by sensitive-memory recovery.

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

Likewise, `REVOKED` and `REJECTED` are terminal **logical** lifecycle states. They make records
ineligible for ordinary Current use while preserving the audit/history contract. They do not claim
physical deletion. Use sensitive-memory recovery only for the separately authorized incident case
where removal from the governed local store is required.

## Storage migration and rollback security boundary

Package rollback and durable-data rollback are different operations. Preserve an operator-owned
backup before a version transition that may affect stored data. Do not run an older FactLane
runtime against a database that may have been migrated or made incompatible by a newer release
unless the newer release explicitly documents that downgrade as safe. Otherwise, recovery of
durable state requires a documented reverse migration or restoration of a verified compatible
backup.

The current storage transition is explicit: a storage-v2 database must be migrated by
`factlane-migrate-storage-v3 migrate --db ... --backup ...`. The operator creates and verifies an
exact storage-v2 backup before contract version 3 is committed. Rollback of that migration uses
`factlane-migrate-storage-v3 restore --db ... --backup ...` with that verified pre-v3 backup.
Running an arbitrary older runtime directly against storage contract v3 is not a supported durable
rollback path. See [Release operations](docs/RELEASE_OPERATIONS.md) for release-specific artifact
verification and the operator transition checklist.

Historical released migration instructions restrict that implementation to the POSIX maintenance
path shipped in the corresponding release. The current **unreleased development source**
additionally contains a qualified Native Windows x64/AMD64 migration and exact pre-v3
rollback path using Windows-specific custody/exclusion primitives. This storage transition is still
different from full Owner Panel database restore and from sensitive-memory incident Recovery.

## Known limits

FactLane is a local service, not a distributed consensus system. Its supported local profile covers
the documented stdio host integration, local SQLite/SQLite-vec storage, local Ollama embeddings,
bounded concurrent operation, crash/restart behavior, backup/restore compatibility, and
production-derived retrieval checks. Operators still need their own backup retention, restore
procedures, monitoring, and deployment-specific recovery plan.

Native Windows x64/AMD64 support in the current development source includes the managed Owner
Panel, its qualified full-restore/permanent-purge paths, and storage-v2→v3 migration with exact
pre-v3 rollback. General sensitive-memory incident Recovery remains separate and continues to depend
on the POSIX `fcntl`/`flock` contract. Do not infer Windows parity for that general Recovery operator.
No Windows ARM64 support claim is made.

The qualification does not establish universal behavior for arbitrary network/FUSE
filesystems, every MCP client, unlimited-duration load, or every language and ranking
workload. Deterministic SQLite capacity exhaustion and permission-read-only behavior were
exercised; a true kernel ENOSPC condition and a true EROFS mount are outside that evidence.
Arabic/mixed-language semantic specificity remains workload-specific. None of these
limitations permits bypassing scope, authority, freshness, or verification policy.

See the [architecture](docs/ARCHITECTURE.md) and
[environment requirements](docs/ENVIRONMENT.md) for implementation and runtime details, and
[release operations](docs/RELEASE_OPERATIONS.md) for versioned install/upgrade/rollback.
