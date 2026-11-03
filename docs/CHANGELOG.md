# FactLane changelog

This changelog records user-visible release changes and upgrade requirements. For exact install,
migration, rollback, and release-identity procedures, use
[Release operations](RELEASE_OPERATIONS.md).

## 0.1.5 release candidate — pending publication

FactLane 0.1.5 is currently a release candidate. The source declares package version `0.1.5`,
Public Contract Revision 3, and storage contract v3, but those source declarations do not create an
official release. The final annotated tag, release commit/tree, wheel and source-distribution
digests, PyPI state, and any site deployment remain pending their separately authorized publication
steps.

### Highlights

- Adds a governed Owner control plane for reviewing and acting on durable memory without expanding
  the public MCP surface beyond the existing five tools.
- Adds direct-Owner `REVOKE` and `REJECT` lifecycle dispositions, contradiction-group compare-and-
  swap protection, durable lifecycle event/operation history, and governed readmission.
- Adds an explicit storage-v2 to storage-v3 migration with a verified pre-v3 backup and a defined
  data rollback path. Existing storage-v2 databases are not silently upgraded during normal startup.
- Adds manual `on_change` freshness recheck with evidence-bound outcomes. FactLane does not add an
  automatic source watcher.
- Packages local Owner CLI and loopback WebUI surfaces while keeping MCP transport command-launched
  `stdio` only.

### Added

- `OwnerControlService` for bounded inventory/detail inspection, conflict review, health and
  retention status, lifecycle actions, manual freshness recheck, and readmission.
- `factlane-owner`, a packaged trusted Owner CLI. Launcher arguments bind the Owner scope and host
  identity; mutation payloads cannot replace that bound authority.
- `factlane-owner-webui`, a packaged local Owner WebUI bound exactly to `127.0.0.1` and one immutable
  scope. Startup issues a short-lived one-use bootstrap token that is exchanged only through the
  `X-FactLane-Bootstrap` header. Authenticated mutations require the resulting session and CSRF
  token. The WebUI is an Owner presentation surface, not an HTTP MCP transport or remote-admin
  service.
- `factlane-migrate-storage-v3`, the explicit trusted migration/restore operator for the v2 to v3
  storage transition.
- Governed readmission for eligible `REVOKED` logical memory after new verification and current CAS
  checks. Readmission creates a governed successor; it is not backup restore and cannot recreate
  content removed by sensitive-memory Recovery.
- Durable lifecycle event and lifecycle operation records for storage-v3 governance decisions.

### Changed

- Public Contract Revision advances from 2 to 3. The public MCP tool set remains exactly:
  `memory_search`, `memory_get`, `memory_store`, `memory_update`, and `memory_status`.
- `memory_update` now supports direct-Owner `REVOKE` and `REJECT` in addition to `REVERIFY` and
  `REPLACE`. Owner dispositions require the exact current revision, contradiction-group observation,
  idempotency protection, and a bounded reason code.
- Storage contract advances from v2 to v3. Storage v3 adds governed-writer fences,
  contradiction-group CAS, lifecycle history, and global idempotency protection across ordinary
  adapter writes and Owner lifecycle operations.
- `REVOKED` and `REJECTED` are terminal logical lifecycle states. They do not represent physical
  erasure of database content.
- `on_change` freshness can now be rechecked through an explicit Owner operation. An unchanged
  observation can renew eligibility; changed, unavailable, indeterminate, stale, mismatched, or
  otherwise unacceptable evidence fails closed for review.

### Upgrade required: storage v2 to v3

Upgrading a durable v0.1.4 database is a **data/schema migration**, not a package-only update.
Normal 0.1.5 startup inspects an existing database and returns `MIGRATION_REQUIRED` for storage v2.
Do not attach the 0.1.5 runtime to that database and expect it to upgrade the schema automatically.

Before migration:

1. Stop or quiesce FactLane so the migration operator has exclusive maintenance access.
2. Choose a new backup path inside an existing private directory owned by the migration operator.
   The backup parent must grant no group or other permissions (`mode & 077 == 0`).
3. Preserve the current v0.1.4 runtime/source identity until migration and post-migration
   verification have completed.

Run the migration only on a supported POSIX maintenance host:

```bash
uv run factlane-migrate-storage-v3 migrate \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

The operator validates exact storage v2 and lineage, creates and verifies the operator-owned backup
and verification manifest with private/no-replace semantics, then applies the v3 governance schema
transactionally and advances the contract version last. Preserve the reported backup path and
SHA-256 as rollback evidence.

If the v3 state must be abandoned before it becomes the accepted durable authority, stop FactLane
and restore the exact verified pre-v3 backup through the migration operator:

```bash
uv run factlane-migrate-storage-v3 restore \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

Installing or restarting v0.1.4 directly against a storage-v3 database is not a supported data
downgrade. See [Release operations](RELEASE_OPERATIONS.md) for the full migration and rollback
contract.

### Security

- Direct-Owner lifecycle decisions are bound to trusted launcher authority, exact revision state,
  contradiction-group CAS, idempotency, and bounded reason codes. An ordinary delegated agent
  remains Candidate-only and cannot promote itself by changing request fields.
- Storage v3 adds connection-local writer fencing and global idempotency checks so stale/untrusted
  writers or key reuse cannot silently cross governance mutation classes.
- The Owner WebUI is loopback-only and uses a one-use bootstrap exchange, an `HttpOnly;
  SameSite=Strict` session cookie, origin/host checks, and CSRF protection for mutations.
- Python/runtime and site-toolchain dependencies received security updates during the 0.1.5 cycle.
  This is not a zero-advisory claim: the current Docusaurus 3.10.2 build chain still has residual
  upstream-blocked advisories in build/dev/preview dependencies. Those packages are part of the
  separately qualified site toolchain; the reviewed production site shape is static output, not a
  Node/Docusaurus application server. Residual build-time and supply-chain risk remains a release
  decision rather than being represented as remediated.
- Sensitive-memory Recovery remains a separate operator-authorized maintenance path. Logical
  `REVOKE`/`REJECT` and readmission do not replace physical sensitive-memory recovery controls.

### Compatibility

- Python requirement remains `>=3.11` and the linked SQLite floor remains `>=3.42.0`.
- MCP transport remains command-launched `stdio` only, with exactly five public tools.
- The bounded native Windows x64/AMD64 **normal-runtime** support established for v0.1.4 is retained.
  This does not extend the maintenance boundary: sensitive-memory Recovery and storage-v2 to v3
  migration/restore remain POSIX-only. No Windows ARM64 support claim is made.
- Existing v0.1.4 embedding-space compatibility rules remain separate from the new storage-v3
  schema migration. A successful package install alone does not establish durable-data
  compatibility.
- The Python package and public website remain separate release surfaces. The qualified Python
  wheel/source distribution does not include the site, and site qualification or deployment does
  not publish the Python package.

### Known limits

- `on_change` does not monitor sources automatically. A trusted Owner must supply a fresh bounded
  observation for manual recheck.
- The Owner WebUI is a local loopback operator surface, not supported remote administration and not
  an HTTP MCP server.
- `REVOKED`/`REJECTED` are logical terminal states; they do not claim physical deletion.
- Readmission is limited to eligible `REVOKED` memory and cannot recover content physically removed
  by sensitive-memory Recovery.
- Storage-v2 to v3 migration/restore and sensitive-memory Recovery are POSIX-only maintenance
  operations.
- Semantic relevance, language quality, filesystem behavior outside the qualified profiles, and
  deployment scale remain workload- and environment-specific rather than universal guarantees.

### Release identity pending publication

The following fields must be filled only from the separately authorized release crossing. Do not
infer them from the moving `main` branch, local RC builds, workflow configuration, or candidate
artifacts.

| Item | 0.1.5 release candidate |
| --- | --- |
| Package version | `0.1.5` |
| Public Contract Revision | `3` |
| Storage contract | `3` |
| Public MCP transport | `stdio` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| Annotated tag | Pending publication |
| Tag object | Pending publication |
| Release commit | Pending publication |
| Release tree | Pending publication |
| Wheel name / SHA-256 | Pending publication |
| Source distribution name / SHA-256 | Pending publication |
| PyPI publication | Pending publication |
| Site Production deployment | Separate publication surface; not implied by this package release |

## 0.1.4

FactLane v0.1.4 preserved Public Contract Revision 2 and the same five-tool MCP surface while adding
the qualified v0.1.3 embedding-space compatibility path, native Windows x64 normal-runtime
qualification, portable Skill bootstrap guidance, stronger package/release controls, and the
bilingual publication surface. Its exact historical release identity and migration notes remain in
[Release operations](RELEASE_OPERATIONS.md).

## 0.1.3

FactLane v0.1.3 was the first official production release. Its exact historical identity and support
boundary remain in [Release operations](RELEASE_OPERATIONS.md).
