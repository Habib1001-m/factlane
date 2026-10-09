# Five MCP tools

FactLane deliberately keeps its public MCP surface small:

```text
memory_search
memory_get
memory_store
memory_update
memory_status
```

The purpose of this page is to explain **when** each tool is useful. The live MCP schema and
`factlane --help-tools` remain authoritative for exact request fields, enums, and signatures.

This is a developer/integrator reference. If you have not connected FactLane yet, start with the
[Quick start](QUICKSTART.md) and establish a safe read-only connection first.

## `memory_search`

Use it to retrieve facts inside one exact scope.

Typical question:

> “What verified project facts are relevant to the current release?”

Important boundaries:

- choose the intended scope;
- `CURRENT` and `REVIEW_HISTORY` serve different purposes;
- current-state retrieval does not make unverified Candidates eligible merely because they rank
  highly;
- bound identity supplied by a trusted host should not be redundantly overridden by the caller.

## `memory_get`

Use it when you already know which logical memory you want to inspect.

It is useful for understanding provenance, lifecycle and revision details rather than performing a
broad relevance search.

## `memory_store`

Use it to contribute one bounded fact when the launcher profile permits writing.

An ordinary `delegated-candidate` connection can contribute a **Candidate**. The tool does not
let that agent mint `VALIDATED_CURRENT` by asserting a privileged verifier identity.

Runtime permission is not content consent. Store only after an explicit user remember/store
request or after the user explicitly authorizes a bounded fact proposed by the agent. Neither path
elevates launcher or verifier authority.

A good contributed fact is:

- bounded;
- attributable;
- scoped;
- accompanied by freshness policy;
- idempotent under retry.

It is not an arbitrary transcript or directory dump.

## `memory_update`

Use it for governed revision operations.

Two important modes are:

- **`REVERIFY`** — trusted verification of the same logical memory identity;
- **`REPLACE`** — a new current revision when the semantic identity changes.

Promotion checks expected record identity and revision so a stale writer cannot silently replace a
newer state.

An ordinary delegated agent does not gain verifier authority simply because this tool is visible.

## `memory_status`

Use it for bounded, read-only observations about a scope's storage and embedding-profile state.

In current **unreleased Development**, the backend status also reports the persisted embedding
compatibility state/binding and whether the observed runtime fingerprint still matches that binding.
`memory_status` does not run a compatibility migration or silently requalify a changed runtime.
When a legacy database is `UNPROVEN` or `INCOMPATIBLE`, safe non-semantic reads (`memory_get`,
`EXACT`, `KEYWORD`) remain available while `SEMANTIC`, `HYBRID`, `memory_store`, and `memory_update`
fail closed with `PROFILE_MISMATCH` until an explicit compatible migration/re-embedding path exists.

It is not a full monitoring platform, backup facility, or maintenance daemon.

## Governed failures

Expected governed failures use the normal MCP result channel with:

- `status=BLOCKED`;
- a stable `error_code`;
- a safe message;
- empty results;
- retry metadata where applicable.

Callers should branch on `error_code`, not scrape exception prose.

Unexpected internal exceptions remain transport errors rather than being relabeled as governed
FactLane results.

## Example: verify connectivity before writing

For a global-user status check that does not require project identity:

```json
{"scope":"GLOBAL_USER"}
```

For a project-scoped current keyword search, a request may look like:

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

When a trusted host has already bound a corresponding project/workflow/tool identity, follow the
live schema and omit conflicting caller-supplied identity.

## Next

- [Architecture](ARCHITECTURE.md) — exact request path, identity, storage and recovery boundaries.
- [Environment](ENVIRONMENT.md) — runtime and embedding-profile compatibility.
