# Environment and compatibility

FactLane is a local Python application with a command-launched MCP server. A compatible
Python interpreter is necessary but not sufficient: the version of **SQLite linked into that
interpreter** determines whether the storage contract can run safely.

## Required runtime

| Component | Requirement |
| --- | --- |
| Python | 3.11 or newer. |
| Linked SQLite | **3.42.0 or newer**. |
| Dependencies | Project `uv.lock`, installed into a project-owned environment. |
| MCP host | Local command-based `stdio` client. |
| Embeddings | Local Ollama reachable on loopback with a matching installed model. |

A CPU-only setup is supported. A GPU, Docker, cloud LLM, remote embedding API, and always-on
FactLane daemon are not prerequisites.

After installing the project, inspect the SQLite version through the **same Python
environment used by FactLane**:

```bash
uv sync --frozen
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
uv run factlane --help
```

Startup fails with `BACKEND_COMPATIBILITY_MISMATCH` when the linked SQLite version is below
3.42.0, before the engine creates or opens its database. This floor is required for
eligible-row filtering inside sqlite-vec KNN and for the FTS5 `secure-delete` capability
used by sensitive-memory recovery. Recovery checks that FTS5 capability independently;
version eligibility does not bypass feature detection.

## Model profiles

FactLane checks the model identity and digest, native/output dimensions, context capability,
and input-size policy against the selected profile. It does not download a model implicitly.
Install the model in your local Ollama instance before launching the server.

| Built-in profile | Ollama model | Notes |
| --- | --- | --- |
| `embeddinggemma-300m-768` | `embeddinggemma:300m` | The profile used in the project's controlled local configuration; 768 output dimensions. |
| `nomic-768` | `nomic-embed-text:latest` | Nomic model with 768 output dimensions. |
| `nomic-512` | `nomic-embed-text:latest` | Same source model, projected to 512 dimensions. |
| `nomic-256` | `nomic-embed-text:latest` | Same source model, projected to 256 dimensions. |
| `minilm-384` | `all-minilm:l6-v2` | Built-in, but observed context limits can reject a maximum-length fact under the no-truncation policy. Check suitability before use. |

For the first profile:

```bash
ollama pull embeddinggemma:300m
uv run factlane --help
```

The profile name must match the selected model and storage configuration. No universal
ranking follows from one project's test data. Assess your own language mix, fact sizes,
hardware, latency, and quality needs; Arabic/mixed-language retrieval and document crowding
remain known qualification gaps.

The only shipped embedding-provider implementation communicates with Ollama on a **loopback
URL** (by default `http://127.0.0.1:11434`). The provider interface permits future
implementations, but a remote endpoint, automatic cloud fallback, or hosted embedding
service is **not** a supported current configuration.

## Host and storage isolation

Use the FactLane executable and dependencies installed for this project rather than
importing packages or a private runtime from the MCP host. A stable, non-secret `--host-id`
is required. `--db` identifies the SQLite database; `--profile` chooses one of the declared
model profiles. `--write-profile` is a trusted **launcher** setting, not a privilege a tool
request can grant itself. Its default is `read-only`; `delegated-candidate` allows only
Candidate contributions.

The upstream backend is pinned in `pyproject.toml` and resolved by `uv.lock`. It supplies
reusable SQLite connection locking, thread offload, WAL, and bounded locked/busy retries;
FactLane supplies scope policy and revision/CAS transactions. Avoid introducing a
host-specific duplicate dependency or a second locking layer when reproducing the
installation.

Ambient backend environment must not weaken that storage posture. FactLane converts a pinned
backend first-import configuration exit into `BACKEND_COMPATIBILITY_MISMATCH` instead of letting
the process terminate outside the governed boundary. `MCP_MEMORY_SQLITE_PRAGMAS` may not change
`journal_mode` away from `WAL`, reduce `synchronous` below `NORMAL`, or reduce `busy_timeout`
below 5000 ms. Schema-qualified forms such as `main.synchronous=OFF` are normalized to the same
required pragma names, so hostile overrides are rejected before database creation; startup also
verifies the effective pragmas after backend initialization.

FactLane's source package and wheel also carry the portable
[`using-factlane` Skill](../skills/using-factlane/SKILL.md). Installing the package does
not configure a particular host or auto-register that Skill.

## Data and deployment limits

A FactLane fact is bounded to **2,000 UTF-8 bytes**. This interface is not a large-directory
crawler, transcript repository, or bulk document index. Very large source collections need a
separate ingestion/extraction stage, which may have different throughput and provider
requirements; no terabyte-scale ingestion rate is claimed here.

Controlled local checks do not constitute authoritative backup/restore acceptance or a
public-production-readiness claim. Keep production data and backup operations under their
own authorization and validation procedures. See [Security](../SECURITY.md),
[Architecture](ARCHITECTURE.md), and the [Quick Start](QUICKSTART.md).
