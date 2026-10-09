# Release operations: install, upgrade, rollback, and reverification

This runbook defines the public operator procedure for consuming a specific FactLane release
and for planning later version transitions. A release identity is the versioned tag together
with its verified commit/tree and the recorded digests of the artifacts published for that tag.
For release maintenance, this runbook requires that published tags are not silently retargeted
and published assets are not replaced to make later documentation agree with different bytes.
Platform permissions can technically mutate those objects, so verify identities rather than
trusting a name alone. The `main` branch may receive documentation or development changes after
a release and must not be used as a substitute for a historical release identity.

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
| Python | `>=3.11` |
| Linked SQLite | `>=3.42.0` |
| Public Contract Revision | `2` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |

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

## Install an exact release

### Option A: versioned source checkout

Use the versioned release tag, and verify its commit/tree, rather than relying on the moving
`main` branch:

```bash
git clone --branch v0.1.3 --depth 1 https://github.com/Habib1001-m/factlane.git
cd factlane

test "$(git rev-parse HEAD)" = "59173b7d9fa3af924c373176411882caa04a6c1d"
test "$(git rev-parse 'HEAD^{tree}')" = "93db940802432b6f9ca2f5513916fd2b84c6393a"

uv sync --frozen
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
uv run factlane --help
uv run factlane --help-tools
```

### Option B: published wheel

Download the release wheel, verify it, then install it into a fresh environment:

```bash
curl -fLO https://github.com/Habib1001-m/factlane/releases/download/v0.1.3/factlane-0.1.3-py3-none-any.whl
printf '%s  %s\n' \
  'daf145cd7754b7c46a0f18ad49cc8872291094c8ac84621fce3cec091f90072a' \
  'factlane-0.1.3-py3-none-any.whl' | sha256sum -c -

python3.11 -m venv .venv
.venv/bin/python -m pip install ./factlane-0.1.3-py3-none-any.whl
.venv/bin/python -m pip check
.venv/bin/factlane --help
.venv/bin/factlane --help-tools
```

The wheel has a pinned Git dependency on `mcp-memory-service`; a fresh online install therefore
needs access to that pinned GitHub revision unless the dependency is already available through
an operator-controlled cache or mirror. The wheel records FactLane's direct requirements, but a
fresh installer may resolve newer compatible **transitive** dependencies than the environment
captured by the repository lockfile. When exact dependency replay matters, use the tagged source
checkout with `uv sync --frozen` and its committed `uv.lock`.

The source distribution is also a supported clean-install surface after verifying its SHA-256:

```bash
curl -fLO https://github.com/Habib1001-m/factlane/releases/download/v0.1.3/factlane-0.1.3.tar.gz
printf '%s  %s\n' \
  '81de371ec23d17109f9267750fed80cd6492cee245681d72630da6868f3cd16b' \
  'factlane-0.1.3.tar.gz' | sha256sum -c -

python3.11 -m venv .venv
.venv/bin/python -m pip install ./factlane-0.1.3.tar.gz
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

   For `v0.1.3`, this must report Public Contract Revision 2 and exactly the five tools listed
   in the release manifest above.

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

   For `v0.1.3`, either applicable path must hash to
   `1c28bc370af0dd89499f8121fb7b611afb4c83773207ce546136abd8018f3e24`.
   A source checkout synchronized with `uv sync --frozen` keeps the authoritative Skill in the
   source tree and need not copy that data file under the virtual-environment prefix.
   Later releases that add Skill references must treat the Skill as a reference set, not a single
   file: record and verify the relative path plus SHA-256 for `SKILL.md` and every shipped
   `references/*` file, and require source/wheel/sdist byte parity for that complete set. File
   presence does not prove host registration or loaded state.
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

See [Quick Start](QUICKSTART.md) for first-time host setup, [Environment](ENVIRONMENT.md) for
runtime requirements, [Architecture](ARCHITECTURE.md) for storage and contract boundaries, and
[Security](../SECURITY.md) for recovery and trust constraints.
