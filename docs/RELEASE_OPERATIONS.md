# Release operations: install, upgrade, rollback, and reverification

This runbook defines the public operator procedure for consuming a specific FactLane release
and for planning later version transitions. A release identity is the versioned tag together
with its verified commit/tree and the recorded digests of the artifacts published for that tag.
For release maintenance, this runbook requires that published tags are not silently retargeted
and published assets are not replaced to make later documentation agree with different bytes.
Platform permissions can technically mutate those objects, so verify identities rather than
trusting a name alone. The `main` branch may receive documentation or development changes after
a release and must not be used as a substitute for a historical release identity.

For user-facing release/development changes and upgrade impact, see the
[changelog](CHANGELOG.md). This runbook remains authoritative for exact recorded release identity,
migration, rollback, installation, and post-install reverification procedures.

### Development package identity

The recorded released Python-package baseline in this repository is `v0.1.6`. The current
development source declares `0.1.7` in `pyproject.toml`, but a local build from a development branch
is not automatically an official release artifact. A recorded release identity is the annotated
tag, its exact commit/tree, the published GitHub Release assets, and the corresponding package bytes
recorded below. If you need to know whether a newer release exists externally, verify that separately
before changing an installation; do not infer it from this moving source tree.

Repository CI classifies this boundary with `scripts/check_release_surface.py`. The Development
classification is intentionally fail-closed for release identity: it records that the source differs
from the released tag and does not assign publication or Production authority. Reusing a published
version string on a later `main` commit does not make that later tree part of the historical release.
A future release must receive its own separately authorized tag and artifact digests before it can be
treated as released truth.

## Release identity contract

For every official release, record all of the following before installation or upgrade:

- version and annotated tag;
- commit and Git tree reached by that tag;
- published wheel and source-distribution names and SHA-256 digests;
- Python and linked-SQLite requirements;
- Public Contract Revision and exact public MCP tool set;
- any data/schema migration requirement and its rollback contract;
- release-specific limits or qualification boundaries.

Do not infer compatibility from a version number alone. Package compatibility, runtime
compatibility, and database/schema compatibility are separate claims.

## FactLane v0.1.3 manifest

`v0.1.3` is the first official FactLane production release. Its released identity is:

| Item | v0.1.3 identity |
| --- | --- |
| Annotated tag | `v0.1.3` |
| Tag object | `d922fcceae7cfbfda239666182e61a5a58278809` |
| Release commit | `59173b7d9fa3af924c373176411882caa04a6c1d` |
| Release tree | `93db940802432b6f9ca2f5513916fd2b84c6393a` |
| Wheel | `factlane-0.1.3-py3-none-any.whl` |
| Wheel SHA-256 | `daf145cd7754b7c46a0f18ad49cc8872291094c8ac84621fce3cec091f90072a` |
| Source distribution | `factlane-0.1.3.tar.gz` |
| Source distribution SHA-256 | `81de371ec23d17109f9267750fed80cd6492cee245681d72630da6868f3cd16b` |
| CPython | `>=3.11` |
| Linked SQLite | `>=3.42.0` |
| Public Contract Revision | `2` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| Portable Skill SHA-256 | `1c28bc370af0dd89499f8121fb7b611afb4c83773207ce546136abd8018f3e24` |

The release assets are published at the
[GitHub Release for `v0.1.3`](https://github.com/Habib1001-m/factlane/releases/tag/v0.1.3).
Verify downloaded bytes against the digests above before installation.

### v0.1.3 support boundary

For `v0.1.3`, the documented supported profile is local command-launched stdio MCP, Python
3.11+, linked SQLite 3.42.0+, supported local Ollama embeddings, and the documented local
POSIX storage/recovery contract.

The release does **not** establish all-language semantic quality, arbitrary MCP-client or
filesystem behavior, or arbitrary deployment scale. Arabic/mixed-language semantic relevance
remains workload-specific. Deterministic SQLite capacity exhaustion and permission-read-only
behavior were exercised; a true kernel `ENOSPC` condition and a true `EROFS` mount were not.

`v0.1.3` is also the first official release. There is no earlier official production release
that FactLane can name as a supported production rollback target. A package installer being
able to replace one version with another is not evidence that an older runtime can safely open
a database that a newer version has modified.

## FactLane v0.1.4 release notes and v0.1.3 transition

`v0.1.4` is the authorized successor to `v0.1.3`. It preserves Public Contract Revision 2 and
exactly the same five public MCP tools. The release adds the qualified v0.1.3 embedding-space
compatibility/migration path, native Windows x64 standard-runtime qualification, portable Skill
bootstrap guidance, stronger package/release-surface controls, and the bilingual publication
surface. Pi remains bounded to the previously accepted native-Windows read-path integration claim;
no broader Pi write-host claim is introduced.

### Migration from v0.1.3

- Preserve an operator-owned backup before opening durable data with v0.1.4.
- Exact known v0.1.3 `embeddinggemma-300m-768` data may migrate metadata-only **only after** the
  integrity-bound compatibility proof passes; existing vectors and per-record embedding provenance
  are preserved rather than rewritten.
- Unknown, mixed, or incompatible embedding identity remains fail-closed for semantic/hybrid
  retrieval and vector-mutating writes; safe exact/keyword/get paths remain available as documented.
- Public Contract Revision remains `2`; the public tool set remains `memory_search`, `memory_get`,
  `memory_store`, `memory_update`, `memory_status`.
- Sensitive-memory recovery remains operator-only and POSIX-only; Windows standard runtime support
  does not expand that recovery boundary.
- Package/runtime rollback to v0.1.3 is not equivalent to a durable-data downgrade guarantee. Do not
  open data modified under v0.1.4 with v0.1.3 unless the applicable data state is proven compatible
  or restored from a verified pre-upgrade backup.

The published v0.1.4 identity is now sealed and recorded as:

| Item | v0.1.4 identity |
| --- | --- |
| Annotated tag | `v0.1.4` |
| Tag object | `4276f3cbae14ebce89030d516f64682aabae59c9` |
| Release commit | `4b2cf5b27d454799dd253c0306d83bd076c9369f` |
| Release tree | `d9a69de5e63419bfb6aade745e25137e07857cbb` |
| Wheel | `factlane-0.1.4-py3-none-any.whl` |
| Wheel SHA-256 | `b1a256d124d74ffd6b9dba4338c171e35b95fa901e232bb1ed59ebd4f0316307` |
| Source distribution | `factlane-0.1.4.tar.gz` |
| Source distribution SHA-256 | `80c01dbf20dd5139350a9434c787c27a468f458397740fd0d85064394be1f5a3` |
| Python | `>=3.11` |
| Linked SQLite | `>=3.42.0` |
| Public Contract Revision | `2` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| Portable Skill `SKILL.md` SHA-256 | `6255ddb8eeea074cd324d1f096d3e043f09a189b46cb3f64c724bb29ecd397a4` |
| Portable Skill `references/host-bootstrap.md` SHA-256 | `05882a0d6716e32cd23ef452c1683a8aff1294abfcc7950fea771320cd211d4c` |

These are historical release facts. They do not provide identity or publication authority to any
later release.

## FactLane v0.1.5 release and v0.1.4 transition

FactLane v0.1.5 is a sealed historical GitHub release with package version `0.1.5`, Public Contract
Revision 3, and storage contract v3. Its recorded tag and GitHub Release asset identities are the
historical evidence and must not be retargeted or replaced to rewrite that history. PyPI publication
did not complete because PyPI rejected the wheel's direct VCS dependency
metadata; v0.1.5 is therefore not available as `factlane==0.1.5` from PyPI. The corrective successor
v0.1.6 fixes only that package-distribution metadata problem. The [0.1.5 changelog entry](CHANGELOG.md)
summarizes the user-visible feature changes; this section defines the transition mechanics.

The release keeps the public MCP transport at **stdio only** and the public MCP surface at exactly the same
five tools. Revision 3 extends `memory_update` with direct-Owner `REVOKE`/`REJECT` modes and adds the
governed Owner control plane around the same stored lifecycle: `OwnerControlService`, the packaged
`factlane-owner` CLI, and the packaged `factlane-owner-webui` loopback launcher. The launcher prints
the bound origin and one-use bootstrap token; the token is exchanged only through the
`X-FactLane-Bootstrap` header and never through a URL or `localStorage`. The WebUI is a local Owner
presentation layer over that service, not an HTTP MCP server and not a sixth MCP tool.

Storage v3 adds terminal logical states `REVOKED` and `REJECTED`, contradiction-group CAS, durable
lifecycle event and operation records, manual `on_change` freshness recheck, and governed readmission.
Terminal lifecycle is logical; it does not claim physical erasure. Readmission is a verified
lifecycle operation for an eligible `REVOKED` memory and is not backup restore or sensitive-Recovery
resurrection.

`on_change` remains evidence-driven rather than automatically monitored. The release does not run a
background watcher that revalidates a source. An Owner supplies a fresh bounded observation to the
manual recheck operation; changed, unavailable, indeterminate, stale, or mismatched evidence fails
closed for review.

The sealed v0.1.5 identity is:

| Item | v0.1.5 identity |
| --- | --- |
| Annotated tag | `v0.1.5` |
| Tag object | `e4275fb33147c3336797bb230312699571a0afe5` |
| Release commit | `eaa75f8f17961cbaaf94d5d623e5a80083dad56f` |
| Release tree | `6cc017b508571be0636f6f4b031aa4ca452893c4` |
| Wheel | `factlane-0.1.5-py3-none-any.whl` |
| Wheel SHA-256 | `9ff8039125cab109c169dacc62a88aae91cbbe2cd12abb0c2661e72db84c4a79` |
| Source distribution | `factlane-0.1.5.tar.gz` |
| Source distribution SHA-256 | `7f997ecb85a519e5c0b44d136a774182f1453d832dda02a795463602d627a0ac` |
| Python | `>=3.11` |
| Linked SQLite | `>=3.42.0` |
| Public Contract Revision | `3` |
| Storage contract | `3` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| PyPI | Not published; terminal direct-VCS metadata rejection |

### Required v2 → v3 storage migration

An existing v0.1.4 storage-v2 database is not silently upgraded by normal v0.1.5 startup. The runtime
fails closed with `MIGRATION_REQUIRED` until the trusted migration operator completes the transition.
Treat this as a **data/schema migration**, not a package-only upgrade. A successful package install
does not authorize opening storage-v2 data with the normal 0.1.5 runtime.
Stop/quiesce FactLane and choose a new backup path inside an **existing private directory owned by
the migration operator** before running. The backup parent must grant no group/other permissions
(`mode & 077 == 0`); the migration does not create or relax that directory for you:

Both this migration and the matching pre-v3 restore path are **POSIX-only maintenance operations**.
Native Windows standard-runtime support does not include v2 → v3 migration or restore.

```bash
uv run factlane-migrate-storage-v3 migrate \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

The operator requires exact storage v2, verifies database integrity and lineage, creates the SQLite
backup in a confidential `0600` same-directory staging inode, reopens that existing inode without
allowing recreation, verifies integrity and contract v2, then atomically publishes the backup with
no replacement. Its verification manifest is published with equivalent private/no-replace semantics.
A pre-existing backup or manifest destination is rejected. If the backup has already been published
but manifest publication fails or is interrupted, the verified backup is retained and the migration
holds before changing the source database; the operator must investigate rather than overwrite it.
After the verified backup and manifest exist, the operator applies the v3 governance schema
transactionally, advances the storage contract version last, then verifies the resulting database as v3.

After migration, preserve the reported backup path and SHA-256 as rollback evidence and re-run the
normal runtime checks before enabling writes.

The migration changes the Python runtime's durable storage contract only. It does not publish the
Python package, deploy the public site, or grant either surface Production authority. Site
qualification/deployment remains a separate publication workflow from wheel/source-distribution
publication.

### v2 → v3 data rollback

If the migration must be rolled back before the v3 state becomes the accepted durable authority,
stop/quiesce the v0.1.5 runtime and restore the exact verified pre-v3 backup through the migration
operator:

```bash
uv run factlane-migrate-storage-v3 restore \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

Restore verifies that the backup is storage contract v2, stages and fsyncs the replacement image,
promotes it under the maintenance lease, removes stale WAL/SHM sidecars, and verifies the restored
database as v2. Only after that data rollback is proven should an operator consider restarting a
v0.1.4 runtime. Installing v0.1.4 directly over a storage-v3 database is not a supported downgrade.

## FactLane v0.1.6 corrective package release

FactLane v0.1.6 preserves the v0.1.5 product behavior and storage/public contracts while correcting
the package dependency metadata that blocked PyPI publication. The direct backend dependency is now
the exact registry requirement `mcp-memory-service==11.10.0`; the reviewed backend runtime bytes and
required class/signature compatibility remain fail-closed.

The sealed v0.1.6 identity is:

| Item | v0.1.6 identity |
| --- | --- |
| Annotated tag | `v0.1.6` |
| Tag object | `75cf56de9cb693ca07f9d750fc08ff22e0f70e0d` |
| Release commit | `e4a71aedaaf4f2e73aeaeaafbdeca60a846d21ad` |
| Release tree | `4664d232810da749c2267fa56dc1568ab2af1f71` |
| Wheel | `factlane-0.1.6-py3-none-any.whl` |
| Wheel SHA-256 | `5cb74af31e6bfe96bc38e0048ea2e8abcde53c5a05470aebbe9ed8305dd46899` |
| Source distribution | `factlane-0.1.6.tar.gz` |
| Source distribution SHA-256 | `cb755605126cbc7f2fcc7c4335e020aa9f6da67f0812dcd7622d99e18c53b84b` |
| Python | `>=3.11` |
| Linked SQLite | `>=3.42.0` |
| Public Contract Revision | `3` |
| Storage contract | `3` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| PyPI | Published as `factlane==0.1.6` |

### Install v0.1.6 from PyPI

On CPython 3.11 or newer, the supported public-index package path is direct and does not require a
Git dependency checkout:

```bash
pip install factlane==0.1.6
```

For an isolated Python 3.11 environment and a dependency/CLI smoke check:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install "factlane==0.1.6"
.venv/bin/python -m pip check
.venv/bin/factlane --help
```

Native Windows PowerShell uses the same package identity with Windows environment paths:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "factlane==0.1.6"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\factlane.exe --help
```

This path was reverified from the public PyPI index after release: it installs FactLane `0.1.6`,
resolves `mcp-memory-service==11.10.0`, passes `pip check`, and exposes the packaged CLI. It is a
package install, not an exact replay of all transitive versions in the repository lockfile. For an
exact dependency replay, use the v0.1.6 tagged source checkout with `uv sync --frozen` and verify the
tag/commit/tree above.

The v0.1.6 transition from an already accepted storage-v3/v0.1.5 state is package/runtime-only; it
does not add a new schema migration. A deployment still on v0.1.4/storage-v2 must complete the v2 →
v3 migration contract above before using the normal v0.1.6 runtime against that durable database.

The public website is a separate release surface. Publishing v0.1.6 to PyPI did not advance the
site's Production release identity or authorize a site deployment.

## FactLane v0.1.7 development transition (unreleased)

The current source tree declares `0.1.7`, but no immutable v0.1.7 tag/artifact/PyPI/site identity is
assigned by this documentation. Treat this section as development/operator guidance, not as a
published-release manifest.

Storage remains contract v3. An already accepted v0.1.5/v0.1.6 storage-v3 database does not require
a new schema transition merely because the runtime moves to the current v0.1.7 development bytes.
A deployment still on exact storage v2 must complete the explicit v2→v3 migration before ordinary
v0.1.7 service.

### Native Windows x64/AMD64 v2 → v3 migration

Unlike the historical v0.1.5 migration implementation above, the current 0.1.7 development tree has
a separate Native Windows x64/AMD64 migration/rollback implementation. Stop the managed service and
use native Windows drive paths for both the database and backup. Do not use WSL or UNC paths.

If FactLane or another process still holds the active database or its WAL/SHM files incompatibly,
the operator fails closed before modifying active storage. Close or quiesce the holder and rerun the
command; do not delete or replace the database or sidecars to bypass the hold.

PowerShell example from an installed/current development environment:

```powershell
factlane-migrate-storage-v3 migrate `
  --db "C:\path\to\state\factlane.sqlite3" `
  --backup "C:\path\to\backup\factlane-pre-v3.sqlite3"
```

The Windows operator verifies exact storage v2 and target custody, maintains continuous exclusion
over the active database/WAL/SHM mutation window, creates and verifies a private pre-v3 backup plus
its verification manifest before active storage is changed, establishes verified rollback state
before the active database is changed, and verifies the resulting v3 image before releasing
maintenance custody. Existing backup or manifest destinations are not silently replaced.

Rollback uses the exact verified pre-v3 backup and its bound verification state:

```powershell
factlane-migrate-storage-v3 restore `
  --db "C:\path\to\state\factlane.sqlite3" `
  --backup "C:\path\to\backup\factlane-pre-v3.sqlite3"
```

The restore path returns the active database to the verified pre-v3 image and verifies the result
before maintenance custody is released. Exact rollback is supported only while the target is still
the immediate migrated-v3 result bound to that backup. Later accepted v3 logical/lifecycle/trust-clock
changes or a backup/target binding mismatch fail closed with `ROLLBACK_HOLD` and do not change data.
Package rollback alone is not durable-data rollback.

If migrate or restore is interrupted, leave the database, backup, and verification files intact and
rerun the same operation with the same paths. Do not manually replace or delete those files; the
operator resumes from verified state when possible or fails closed when it cannot prove a safe resume.

This Windows migration/rollback contract is distinct from both the managed Owner Panel's full
database restore workflow and general sensitive-memory incident Recovery. The latter remains the
separate POSIX-only `SensitiveMemoryRecoveryOperator` contract.

### Managed Owner Panel

The current 0.1.7 development tree also provides the managed `factlane ... webui` lifecycle on
Native Windows x64/AMD64. See [Quick Start](QUICKSTART.md) for the status/start/open workflow and
[Security](../SECURITY.md) for session, sensitive-approval, permanent-purge, and full-restore trust
boundaries. These local capabilities do not assign a public v0.1.7 release identity.

## Install an exact release

Copy the selected release manifest's exact identity into shell variables before using either
installation path. Do not substitute values from moving `main`, a package index listing, or memory:

```bash
RELEASE_TAG="<annotated tag>"
RELEASE_COMMIT="<release commit>"
RELEASE_TREE="<release tree>"
WHEEL_NAME="<wheel filename>"
WHEEL_SHA256="<wheel SHA-256>"
SDIST_NAME="<source-distribution filename>"
SDIST_SHA256="<source-distribution SHA-256>"
```

### Option A: versioned source checkout

Use the versioned release tag, and verify its commit/tree, rather than relying on the moving
`main` branch:

```bash
git clone --branch "$RELEASE_TAG" --depth 1 https://github.com/Habib1001-m/factlane.git
cd factlane

test "$(git rev-parse HEAD)" = "$RELEASE_COMMIT"
test "$(git rev-parse 'HEAD^{tree}')" = "$RELEASE_TREE"

uv sync --frozen
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
uv run factlane --help
uv run factlane --help-tools
```

### Option B: published wheel

Download the release wheel, verify it, then install it into a fresh environment:

```bash
curl -fLO "https://github.com/Habib1001-m/factlane/releases/download/${RELEASE_TAG}/${WHEEL_NAME}"
printf '%s  %s\n' "$WHEEL_SHA256" "$WHEEL_NAME" | sha256sum -c -

python3.11 -m venv .venv
.venv/bin/python -m pip install "./${WHEEL_NAME}"
.venv/bin/python -m pip check
.venv/bin/factlane --help
.venv/bin/factlane --help-tools
```

The wheel records FactLane's direct requirements, but the dependency origin is release-specific:
do not assume that a selected release uses a Git source or a package-index source without checking
that release's metadata and notes. A fresh installer may also resolve newer compatible
**transitive** dependencies than the environment captured by the repository lockfile. When exact
dependency replay matters, use the tagged source checkout with `uv sync --frozen` and its committed
`uv.lock`.

The source distribution is also a supported clean-install surface after verifying its SHA-256:

```bash
curl -fLO "https://github.com/Habib1001-m/factlane/releases/download/${RELEASE_TAG}/${SDIST_NAME}"
printf '%s  %s\n' "$SDIST_SHA256" "$SDIST_NAME" | sha256sum -c -

python3.11 -m venv .venv
.venv/bin/python -m pip install "./${SDIST_NAME}"
```

The portable `using-factlane` Skill is shipped with the package, but package installation does
not register the Skill with an MCP host automatically.

## Upgrade procedure for later releases

Treat every version transition as one of three different change classes before doing any work:

1. **Package/runtime only** — Python package, dependencies, documentation, or host launch
   configuration changes while the existing database format remains explicitly compatible.
2. **Data/schema migration** — the new release changes stored schema or data semantics and
   supplies an explicit migration procedure.
3. **Compatibility not established** — the release does not state that an existing database can
   be used by the new runtime. Do not infer a migration path.

### Pre-upgrade checklist

Before switching a running deployment:

1. Record the current version, exact source/artifact identity, Python version, linked SQLite
   version, embedding profile, and host launch configuration.
2. Read the target release notes and this runbook. Identify whether the transition is
   package-only or includes an explicit data/schema migration.
3. Preserve the current runtime or source checkout so a package-level rollback remains
   mechanically possible.
4. Create and verify an operator-owned backup of any database whose state matters. Backup
   retention and restore procedures remain deployment responsibilities.
5. Define the rollback target **before** changing data. If the release does not document a
   compatible rollback target, treat data rollback as unsupported.
6. Stop or quiesce FactLane before any release-specific procedure that says exclusive database
   access is required.

### Install the target beside the current runtime

Prefer a fresh virtual environment or separate versioned source checkout instead of mutating
the active environment in place. Verify the target artifact digest or tag/commit/tree first,
then install using that release's declared dependency path. For an exact lockfile replay, use
the versioned source checkout and its committed `uv.lock`.

Do **not** attach the new runtime to an existing production database merely because package
installation succeeded. Use an existing database only when the target release explicitly states
that the source version/database format is compatible or provides a qualified migration path.

## Reverification after install or upgrade

Before enabling writes, verify the release as installed:

1. Confirm package version and dependencies:

   ```bash
   python -c 'from importlib.metadata import version; print(version("factlane"))'
   python -m pip check
   ```

2. Confirm the linked SQLite runtime from the same interpreter:

   ```bash
   python -c 'import sqlite3; print(sqlite3.sqlite_version)'
   ```

3. Check the offline public-tool reference:

   ```bash
   factlane --help-tools
   ```

   Compare the reported Public Contract Revision with the selected release manifest. The supported
   public MCP surface remains exactly `memory_search`, `memory_get`, `memory_store`, `memory_update`,
   and `memory_status`; tool visibility does not grant write authority.

4. Confirm the portable Skill bytes for the installation surface. For a tagged source checkout,
   verify the tracked source copy:

   ```bash
   sha256sum skills/using-factlane/SKILL.md
   ```

   For a wheel or source-distribution install into the `.venv` used in the examples above,
   verify the installed data file:

   ```bash
   sha256sum .venv/share/factlane/skills/using-factlane/SKILL.md
   ```

   Compare the applicable bytes with the Skill/reference-set digests recorded by the selected
   release. A source checkout synchronized with `uv sync --frozen` keeps the authoritative Skill in
   the source tree and need not copy that data file under the virtual-environment prefix. Releases
   that ship Skill references must treat the Skill as a reference set, not a single file: record and
   verify the relative path plus SHA-256 for `SKILL.md` and every shipped `references/*` file, and
   require source/wheel/sdist byte parity for that complete set. File presence does not prove host
   registration or loaded state.
5. Start the configured stdio host in the intended write profile and confirm tool discovery
   matches the release contract. Tool visibility does not grant write authority.
6. When validating a new deployment path, prefer a disposable database for smoke tests. If a
   release includes a data/schema migration, perform the release-specific integrity and
   migration checks before normal service resumes.

## Rollback procedure

Rollback has two different meanings and they must not be conflated.

### Package/runtime rollback

If no incompatible data/schema change has occurred, stop the new runtime and switch back to the
preserved prior environment or versioned checkout. Reverify its package identity, linked SQLite,
public contract, and host configuration before resuming service.

For `v0.1.3`, there is no earlier official production release to use as an official rollback
target. A package manager being able to replace installed package versions does not create such a
target or prove a safe database downgrade.

### Data/schema rollback

Never run an older FactLane binary against a database that may have been migrated or otherwise
made incompatible by a newer release unless the newer release explicitly declares that downgrade
safe. A data/schema rollback requires the release-specific reverse migration or restoration of a
verified pre-upgrade backup under a runtime that is documented as compatible with that backup.

For `v0.1.3`, no cross-version production-data migration or downgrade contract is claimed. If a
deployment needs such a path, validate it for that deployment before relying on it.

## Release-author checklist for future versions

Every later official release should use this checklist:

1. Before publication, state the required Python/SQLite/runtime profile and the exact public
   contract revision/tool set.
2. Classify the transition from each supported prior official version as package-only,
   data/schema migration, or unsupported.
3. If data/schema changes exist, document backup prerequisites, forward migration, post-migration
   integrity checks, and the supported rollback or restore path before release.
4. Freeze one exact release tree; build wheel and source distribution from that tree; verify clean
   installation, package metadata, runtime-source parity, and complete portable Skill reference-set
   parity (relative paths plus SHA-256, not `SKILL.md` alone).
   Use `scripts/build_qualified_package.py` for qualification builds: it exports the exact committed
   tree into a disposable directory before invoking `uv build`, so ignored or stale `build/` and
   `*.egg-info/` state in a reused checkout cannot enter the release payload. Do not use a direct
   `uv build --wheel` from a reused worktree as release evidence.
5. Reverify the configured MCP host and the exact public tool set before publication.
6. Publish the tag and assets only after the release gates pass. Do not change an already-published
   tag or replace its assets to make later documentation agree with it.
7. Record the final tag object, commit, tree, artifact names, and SHA-256 digests in the versioned
   manifest. If those identities are not knowable until publication, record them in a follow-up
   documentation-only `main` commit rather than mutating the released tag/tree.
8. Carry forward only evidence-backed support claims. Arabic/mixed-language semantic quality,
   kernel `ENOSPC`, true `EROFS`, filesystem behavior, client coverage, and scale limits must not
   be broadened without direct evidence.
9. Do not retarget a previous release tag or replace its published assets. Continue verifying
   historical releases by their recorded commit/tree and artifact digests. A documentation
   maintenance change on `main` does not rewrite a historical release.
10. If a release accepts an existing embedding space across a runtime/provider transition, record
    the semantic-compatibility revision and identity separately from runtime provenance, freeze the
    qualified cross-space anchor bundle as integrity-bound release material, and require byte parity
    for that bundle across source, wheel, and source distribution.
11. For every supported legacy embedding transition, prove whether migration is metadata-only or
    requires explicit re-embedding. A metadata-only transition must demonstrate zero vector rewrite
    and preservation of per-record embedding provenance. An incompatible or unprovable transition
    must preserve durable facts and fail semantic/vector-mutating operations closed until an explicit
    re-embedding or verified restore path is authorized.

See [Quick Start](QUICKSTART.md) for first-time host setup, [Environment](ENVIRONMENT.md) for
runtime requirements, [Architecture](ARCHITECTURE.md) for storage and contract boundaries, and
[Security](../SECURITY.md) for recovery and trust constraints.
