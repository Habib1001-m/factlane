# Quick start

This guide sets up a local FactLane MCP server and connects it to an agent. The server is a
**stdio process started by the MCP host**, not a web service or an interactive terminal
application. It stores bounded facts; it is not a transcript importer.

## 1. Install and check the runtime

Install Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and [Ollama](https://ollama.com/)
on the machine that will run FactLane. The Python environment used for FactLane must link to
**SQLite 3.42.0+**.

```bash
git clone https://github.com/Habib1001-m/factlane.git
cd factlane
uv sync --frozen
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
uv run factlane --help
uv run factlane --help-tools
```

The SQLite check above uses the project interpreter. A system-wide `python` or `sqlite3`
executable may report a *different* version. Below the floor, FactLane returns
`BACKEND_COMPATIBILITY_MISMATCH` before creating or opening its database. Python version
alone does not satisfy the storage contract.

## 2. Install a supported embedding model

The following example uses the built-in `embeddinggemma-300m-768` profile:

```bash
ollama pull embeddinggemma:300m
```

Start the local Ollama service using your installation's normal procedure. The default
FactLane connection is `http://127.0.0.1:11434`. Model identity, digest, dimensions, and
input capacity must match the profile; a model is not downloaded or replaced automatically.

Other built-in profiles, including Nomic alternatives, are listed in
[Environment and compatibility](ENVIRONMENT.md). Model suitability depends on your fact
sizes, language mix, latency requirements, and hardware. The selected example is not a
universal model recommendation. FactLane does not currently support remote embedding
endpoints.

## 3. Choose the launch profile

A FactLane server invocation has three required pieces of configuration: `--db` (the local
SQLite file), `--host-id` (a stable non-secret label for the launcher), and an embedding
`--profile` (it defaults to `nomic-768`, so set it explicitly when using the example
model).

For an MCP client that may contribute unverified Candidates, use arguments equivalent to:

```bash
uv run factlane \
  --db ./factlane.sqlite3 \
  --profile embeddinggemma-300m-768 \
  --host-id example-host \
  --write-profile delegated-candidate
```

Do not run that command as a REPL; configure your MCP host to launch it over stdin/stdout.
If the host starts FactLane from another working directory, use **absolute** executable and
database paths.

The `--write-profile` flag is a **trusted launcher setting**:

| Launcher profile | What it permits |
| --- | --- |
| Omitted / `read-only` | Search, get, and status; no memory mutation. |
| `delegated-candidate` | Normal agent can `memory_store` a `CANDIDATE`; it cannot self-verify or update. |
| `owner-current`, `repo-verifier`, `automated-verifier` | Restricted trusted operator/verifier profiles; do not configure for an ordinary agent to acquire more authority. |

An Owner's approval of content does not change the authorization of a running agent.
Candidate promotion is a separate, trusted `memory_update` operation.

### Codex (tested stdio host)

An example `~/.codex/config.toml` entry is:

```toml
[mcp_servers.factlane]
command = "/absolute/path/to/factlane/.venv/bin/factlane"
args = [
  "--db", "/absolute/path/to/state/factlane.sqlite3",
  "--profile", "embeddinggemma-300m-768",
  "--host-id", "codex",
  "--write-profile", "delegated-candidate"
]
enabled = true
```

Remove the `--write-profile` argument pair if the connection should be read-only. Reload
your installed Codex version's MCP configuration and confirm that the five FactLane tools
appear. An agent's use of those tools should follow the portable
[using-factlane Skill](../skills/using-factlane/SKILL.md), installed through your host's
supported Skill mechanism.

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
      - "embeddinggemma-300m-768"
      - "--host-id"
      - "hermes"
      - "--write-profile"
      - "delegated-candidate"
```

Use `read-only` or omit the write-profile pair for a connection that must not write. Reload
Hermes's MCP configuration and check tool discovery. The portable Skill is included in
FactLane's source and package artifacts but is **not automatically registered** with either
host by installing the wheel.

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

If search returns no result, do not treat that as permission to invent a fact. To contribute
an actual Candidate, consult `factlane --help-tools` or the live MCP schema for the complete
required `source_provenance`, `freshness_policy`, `memory_type`, and `idempotency_key`
fields. Use one bounded, attributable fact, not a transcript or an arbitrary directory dump.

A trusted verifier reviewing a Candidate uses `REVIEW_HISTORY` to inspect it and
`memory_update` with `mode=REVERIFY`, `expected_revision`, and the Candidate's
`expected_record_id` to promote it. A delegated agent has no verifier grant, even with user
approval in chat.

## Limits and next references

Facts are limited to 2,000 UTF-8 bytes. FactLane is not a raw-corpus indexer, backup
service, or remote embedding gateway. Reproduction on this example profile does not
establish language quality or production-scale throughput for your data. Sensitive-memory
recovery is a separate, operator-authorized procedure, **not** a public MCP tool.

See the [README](../README.md) for product orientation, [Architecture](ARCHITECTURE.md) for
exact data flow, [Environment](ENVIRONMENT.md) for runtime and profiles, and
[Security](../SECURITY.md) for trust and recovery boundaries. The live MCP schema and
`--help-tools` output are authoritative for request signatures and enums.
