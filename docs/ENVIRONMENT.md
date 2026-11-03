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

The current supported operating boundary is:

| Surface | Current support |
| --- | --- |
| Public MCP surface | Exactly five tools: search, get, store, governed update, and status. |
| MCP transport | Command-launched stdio only. |
| Durable storage | Storage contract v3; exact storage-v2 state requires explicit migration. |
| Owner control | Local `OwnerControlService`, packaged `factlane-owner`, and loopback-only `factlane-owner-webui`. |
| Normal Windows runtime | Native Windows x64/AMD64 for ordinary package/runtime behavior. |
| Sensitive Recovery / storage migration maintenance | POSIX maintenance-lock contract; no Windows maintenance claim. |

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

FactLane checks the model identity/family, native/output dimensions, context capability, and
input-size policy against the selected profile. Ollama must be **0.22.1 or newer**. The observed
Ollama version and model digest are runtime provenance rather than, by themselves, proof that an
existing vector space is semantically compatible. FactLane does not download a model implicitly;
install the model in your local Ollama instance before launching the server.

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
hardware, latency, and quality needs. Arabic/mixed-language EXACT and KEYWORD behavior is
supported by the same Unicode handling as other text, while semantic relevance remains
workload-specific and must be validated against the intended corpus. Source-diverse selection
reduces one document-crowding mechanism but is not a universal ranking guarantee.

The only shipped embedding-provider implementation communicates with Ollama on a **loopback
URL** (by default `http://127.0.0.1:11434`). The provider interface permits future
implementations, but a remote endpoint, automatic cloud fallback, or hosted embedding
service is **not** a supported current configuration.

The compatibility binding records semantic identity separately from runtime provenance. Semantic
identity includes the base model/family, source/output dimensions,
document/query prefixes, normalization policy, cosine metric, projection revision, and FactLane
embedding-compatibility revision. For the packaged `embeddinggemma-300m-768` legacy compatibility
path, an integrity-bound qualified anchor set compares candidate document/query vectors directly
with frozen legacy vectors before migration. The bundle is verified by SHA-256 before use.
The Ollama version recorded in that bundle is frozen environment provenance; it is not a direct
per-request version attestation or a semantic-compatibility key.

When that legacy proof passes, migration is metadata-only: existing vectors and each record's
`embedding_profile_id`, `embedding_model_digest`, and `embedding_output_dimension` are preserved.
If compatibility is missing, unknown, mixed, or fails qualification, durable facts remain readable
through `memory_get`, `EXACT`, and `KEYWORD`, and `memory_status` can report the degraded state; semantic
or hybrid retrieval, writes, and vector-mutating maintenance fail with `PROFILE_MISMATCH`. There is
no automatic transform or re-embedding fallback. Re-embedding is an explicit operator/release
migration decision, not a startup side effect.

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
not configure a particular host or auto-register that Skill. The Skill's
`references/host-bootstrap.md` defines the host-neutral inspection/registration sequence and keeps
`installed`, `configured`, `present`, `registered`, `discoverable`, and `loaded` as distinct
evidence states rather than treating file presence as host activation.

## Storage transitions

Python-package installation success does not by itself prove that a database created or
modified by another FactLane version is compatible. Treat package/runtime compatibility and
data/schema compatibility as separate release contracts. Preserve the current environment and
a verified operator-owned backup before any version transition that may affect durable data,
and do not run an older binary against a potentially migrated database unless the target
release explicitly documents that downgrade as safe.

Ordinary service expects storage contract v3. Exact storage-v2 state is inspected without silent
migration and returns `MIGRATION_REQUIRED`. The operator must run
`factlane-migrate-storage-v3 migrate` with a separate backup destination. Migration verifies the v2
database and lineage, creates and integrity-checks an exact v2 backup, applies the v3 schema and
writer fences transactionally, advances the contract version last, and verifies v3 after commit.

Rollback of that data transition means `factlane-migrate-storage-v3 restore` from the verified
pre-v3 backup. Package rollback alone is not sufficient once durable data has reached contract v3.
See [Release operations](RELEASE_OPERATIONS.md) for release-specific identity and transition
procedures.

## Data and deployment limits

A FactLane fact is bounded to **2,000 UTF-8 bytes**. This interface is not a large-directory
crawler, transcript repository, or bulk document index. Very large source collections need a
separate ingestion/extraction stage, which may have different throughput and provider
requirements; no terabyte-scale ingestion rate is claimed here.

The normal runtime is supported on native Windows x64/AMD64 with Python 3.11+, linked SQLite
3.42.0+, the pinned sqlite-vec Windows AMD64 artifact, command-launched stdio MCP, and the same
five-tool authority model. This boundary covers normal package import/startup, read-only and
Candidate workflows, verifier and Owner control logic, WAL/busy behavior, cross-process
CAS/concurrency, fresh-process durability, and valid Windows paths. No Windows ARM64 support claim
is made.

Sensitive-memory Recovery and storage-v2-to-v3 migration/restore are separate maintenance paths.
They rely on the POSIX `fcntl`/`flock` maintenance-lock contract and therefore do not inherit the
normal Windows runtime claim. Where that locking capability is unavailable, maintenance fails
closed before mutation without making the ordinary storage module unimportable. The Owner WebUI is
loopback-only and is a local operator surface, not an MCP transport or remotely supported admin
service.

This is a bounded support statement, not a universal deployment claim. Validate your own
language/ranking workload and operational scale, and keep production data, backup retention,
monitoring, and recovery operations under their own authorization and runbooks. See
[Security](../SECURITY.md), [Architecture](ARCHITECTURE.md), and the
[Quick Start](QUICKSTART.md). Release-specific installation and transition procedures are in
[Release operations](RELEASE_OPERATIONS.md).
