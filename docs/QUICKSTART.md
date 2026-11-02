# Quick start

This guide sets up a local FactLane MCP server and connects it to an agent. The server is a
**stdio process started by the MCP host**, not a web service or an interactive terminal
application. It stores bounded facts; it is not a transcript importer.

## What success looks like

For a first setup, keep the goal narrow. You are done when:

1. the compatible MCP host can launch FactLane;
2. exactly five FactLane tools appear;
3. a read-only `memory_status` request succeeds for an exact scope.

Start read-only. Enable Candidate writes only after basic connectivity works.

## 1. Select an exact published release and check the runtime

Before changing the machine, distinguish **inspection** from **mutation**. Reading the existing
host configuration, installed runtime identity, storage paths, and release documentation is
read-only. Cloning or installing packages, pulling a model, editing host configuration, registering
a Skill, creating or migrating storage, or changing a launcher profile mutates the environment and
requires the applicable operator/user authority. An agent that lacks that authority should stop at
guidance and must not claim that installation or configuration occurred.

Use [Release operations](RELEASE_OPERATIONS.md) to identify the current published FactLane release.
Record its exact tag plus the commit/tree or artifact digests documented there. Do not install from
the moving `main` branch and do not infer a release identity from the package version in an arbitrary
checkout.

For a versioned source installation, set `FACTLANE_TAG` to the published tag named by Release
operations, then clone that exact tag:

```bash
FACTLANE_TAG='<published-tag-from-release-operations>'
git clone --branch "$FACTLANE_TAG" --depth 1 https://github.com/Habib1001-m/factlane.git
cd factlane
```

Verify the checkout commit/tree against the values recorded for that tag in Release operations
before continuing. If you prefer a published wheel or source distribution, follow the digest-checked
installation procedure there instead of translating this source-checkout example by guesswork.

Install the Python, [`uv`](https://docs.astral.sh/uv/), and [Ollama](https://ollama.com/) prerequisites
declared by the selected release and [Environment and compatibility](ENVIRONMENT.md). Verify the
SQLite library linked into the same Python environment that will run FactLane:

The current supported product profile requires a linked SQLite runtime **3.42.0+**. Still verify the
selected release's manifest and Environment boundary rather than assuming a future release keeps the
same floor.

```bash
uv sync --frozen
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
uv run factlane --help
uv run factlane --help-tools
```

The SQLite check above uses the project interpreter. A system-wide `python` or `sqlite3`
executable may report a *different* version. If the linked runtime is below the selected release's
required floor, FactLane returns
`BACKEND_COMPATIBILITY_MISMATCH` before creating or opening its database. Python version
alone does not satisfy the storage contract.

### Inspect existing storage before launch

Choose the intended database path **before** starting the new runtime. If no database exists there,
continue with a new read-only setup. If storage already exists, do not attach the newly installed
runtime merely because package installation succeeded. First record the existing deployment/runtime
identity and storage provenance, then use the selected release's compatibility and transition rules
in [Release operations](RELEASE_OPERATIONS.md) to determine whether that exact database may be opened
directly or requires an explicit migration.

If compatibility cannot be established, stop. If a compatibility check or controlled startup
returns `MIGRATION_REQUIRED`, stop normal service rather than retrying around the hold. Preserve a
verified operator-owned backup using the release-specific procedure, then follow the migration and
rollback instructions in Release operations exactly. Do not let normal MCP startup perform or
improvise a storage migration.

## 2. Install a supported embedding model

Choose a supported embedding profile from [Environment and compatibility](ENVIRONMENT.md), install
the exact model that profile requires, and start the local Ollama service using your installation's
normal procedure. Do not guess a profile/model pair from an older example. Model identity, digest,
dimensions, and input capacity must match the selected profile; FactLane does not download or
replace a model automatically.

Use the endpoint and provider boundary documented for the selected release. Do not introduce a
remote embedding endpoint or fallback unless that release explicitly supports it.

## 3. Choose the launch profile

A FactLane server invocation has three required pieces of configuration: `--db` (the local
SQLite file), `--host-id` (a stable non-secret label for the launcher), and an embedding
`--profile`. Select the profile from the current Environment documentation rather than relying on
an implicit or historical default.

For the first connection, keep the launcher read-only by omitting `--write-profile`:

```bash
FACTLANE_PROFILE='<supported-profile-from-environment>'
uv run factlane \
  --db ./factlane.sqlite3 \
  --profile "$FACTLANE_PROFILE" \
  --host-id example-host
```

Do not run that command as a REPL; configure your MCP host to launch it over stdin/stdout.
If the host starts FactLane from another working directory, use **absolute** executable and
database paths.

Omitting `--write-profile` keeps this first connection read-only. Do not add a write profile until
the read-only connectivity check in step 4 succeeds.

### Codex (tested stdio host)

An example `~/.codex/config.toml` entry is:

```toml
[mcp_servers.factlane]
command = "/absolute/path/to/factlane/.venv/bin/factlane"
args = [
  "--db", "/absolute/path/to/state/factlane.sqlite3",
  "--profile", "<supported-profile>",
  "--host-id", "codex"
]
enabled = true
```

Reload your installed Codex version's MCP configuration and confirm that the five FactLane tools
appear. An agent's use of those tools should follow the portable
[using-factlane Skill](../skills/using-factlane/SKILL.md), installed through your host's
supported Skill mechanism. The Skill distribution includes `SKILL.md` and
`references/host-bootstrap.md`. File presence proves only that the Skill bytes are present; it does
not prove that the host registered, discovered, or loaded the Skill. Keep `present`, `registered`,
`discoverable`, and `loaded` as separate observed states and use the bootstrap reference for the
evidence expected for each state.

### Hermes (tested stdio host)

Example `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  factlane:
    command: "/absolute/path/to/factlane/.venv/bin/factlane"
    args:
      - "--db"
      - "/absolute/path/to/state/factlane.sqlite3"
      - "--profile"
      - "<supported-profile>"
      - "--host-id"
      - "hermes"
```

Reload Hermes's MCP configuration and check tool discovery. The portable Skill is included in
FactLane's source and package artifacts but is **not automatically registered** with either
host by installing the wheel. Use the host's supported Skill mechanism and report `present`,
`registered`, `discoverable`, and `loaded` as separate observed states.

### Another MCP client

Configure the same `factlane` executable and arguments in a client that supports local
command-based **stdio MCP**. Give each trusted host a suitable stable `--host-id`. Other
clients are protocol-compatible in principle; they have not all been qualified individually.
HTTP/SSE and streamable HTTP are not supported by the current server.

## 4. Verify a connection before writing

The MCP host should discover exactly:

```text
memory_search
memory_get
memory_store
memory_update
memory_status
```

`memory_store` or `memory_update` being *listed* does not confer permission: the launcher
profile still controls writes. Ask the client to call `memory_status` with an exact scope.
For a global-user check that does not require a project ID:

```json
{"scope":"GLOBAL_USER"}
```

If that request succeeds, the safe first-run objective is complete: the host can launch FactLane,
the public tool surface is visible, and a read-only request can cross the configured runtime path.

### Identity and retrieval notes

The examples below matter when you move beyond the first connectivity check.

For an **unbound** project-scoped search, a request can look like this:

```json
{
  "scope": "PROJECT",
  "project_id": "example-project",
  "intent_class": "CURRENT_PROJECT_STATE",
  "query": "What release constraints are recorded?",
  "retrieval_mode": "CURRENT",
  "retrieval_mode_kind": "KEYWORD"
}
```

When your trusted host has already bound a project or workflow identity, **omit the
corresponding bound ID from the request**. FactLane supplies it at the gateway and rejects a
conflicting explicit ID with `BOUND_CONTEXT_IDENTITY_MISMATCH`. `CROSS_PROJECT_WORKFLOW` is
different: it forbids the presence of *all* project, worktree, workflow, and agent identity
keys and does not query all projects automatically.

For `CURRENT` semantic or hybrid search, FactLane selects across distinct validated stored
provenance sources before filling repeated-source slots. The highest-ranked eligible result
is preserved, returned relevance scores are not rewritten, and exact, keyword-only, and
`REVIEW_HISTORY` retrieval keep their existing ordering semantics.

Governed FactLane failures use the same MCP result channel as successful calls and return
`status=BLOCKED` with a stable `error_code`, a safe `message`, empty `results`, and
`audit.retryable`. Branch on `error_code`; do not scrape exception prose. Unexpected internal
exceptions are still transport errors rather than governed FactLane results.

## 5. Enable Candidate writes only after read-only works

The `--write-profile` flag is a **trusted launcher setting**:

| Launcher profile | What it permits |
| --- | --- |
| Omitted / `read-only` | Search, get, and status; no memory mutation. |
| `delegated-candidate` | Normal agent can `memory_store` a `CANDIDATE`; it cannot self-verify or update. |
| `owner-current`, `repo-verifier`, `automated-verifier` | Restricted trusted operator/verifier profiles; do not configure for an ordinary agent to acquire more authority. |

User approval of content does not change the authorization of a running agent. Candidate promotion
is a separate, trusted `memory_update` operation. Content consent has two supported entry paths:
an explicit request such as “remember this,” or an agent-proposed reusable fact that the user then
explicitly authorizes. Both authorize only the bounded Candidate content, subject to the active
runtime/privacy boundary; neither authorizes changing the launcher profile or becoming a verifier.

If the connected agent should be allowed to contribute **unverified Candidates**, add the trusted
launcher setting:

```text
--write-profile delegated-candidate
```

That profile permits `memory_store` to contribute a Candidate. It does **not** let the ordinary
agent verify or promote its own contribution.

If the connection does not need to contribute memory, leave the write profile omitted/read-only.

If search returns no result, do not treat that as permission to invent a fact. To contribute
an actual Candidate, consult `factlane --help-tools` or the live MCP schema for the complete
required `source_provenance`, `freshness_policy`, `memory_type`, and `idempotency_key`
fields. Use one bounded, attributable fact, not a transcript or an arbitrary directory dump.
If the proposed fact originated with the agent rather than an explicit user request, present the
bounded fact and obtain explicit authorization before calling `memory_store`. Never perform
autonomous post-turn storage.

A trusted verifier reviewing a Candidate uses `REVIEW_HISTORY` to inspect it and
`memory_update` with `mode=REVERIFY`, `expected_revision`, and the Candidate's
`expected_record_id` to promote it. A delegated agent has no verifier grant, even with user
approval in chat. `REVERIFY` refreshes verification without changing the memory's
contradiction identity; use `REPLACE` rather than changing `memory_type` or subject when a
semantic reclassification is intended.

### Optional trusted Owner control path

The normal agent onboarding path ends at the five public MCP tools above. Some published releases
may additionally ship local Owner control surfaces such as `factlane-owner` or
`factlane-owner-webui`. Treat those as **optional trusted-operator interfaces** only when the
selected release documents and installs them; do not assume they exist from this Quick Start.

Owner control does not create a sixth MCP tool and does not turn the loopback WebUI into an HTTP MCP
transport. Bind Owner operations to the exact trusted scope and use current inventory/detail or
conflict observations for revision and Group-CAS proof. Do not attempt Owner lifecycle operations
from a delegated-agent session, and do not treat content approval in chat as Owner or machine-level
authority.

Where supported, `REVOKED` and `REJECTED` are logical lifecycle states rather than physical erasure.
Manual recheck and readmission are explicit Owner operations, not background source monitoring,
database restore, or recovery resurrection. Follow the selected release's Owner and security
documentation for exact commands and mutation envelopes.

## 6. Limits and next references

Facts are limited to 2,000 UTF-8 bytes. FactLane is not a raw-corpus indexer, backup
service, or remote embedding gateway. Reproduction on this example profile does not
establish language quality or production-scale throughput for your data. Sensitive-memory
recovery is a separate, operator-authorized procedure, **not** a public MCP tool; use
[Security](../SECURITY.md) for the recovery trust boundary.

Continue with [Five MCP tools](TOOLS.md) for the developer-facing tool guide.

For deeper evaluation, see [Architecture](ARCHITECTURE.md) for exact data flow,
[Environment](ENVIRONMENT.md) for runtime and profiles,
[Release operations](RELEASE_OPERATIONS.md) for versioned install/upgrade/rollback, and
[Security](../SECURITY.md) for trust and recovery boundaries. The live MCP schema and
`--help-tools` output are authoritative for request signatures and enums.
