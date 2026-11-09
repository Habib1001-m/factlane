# Five MCP tools

FactLane keeps its public MCP surface deliberately small:

1. `memory_search`
2. `memory_get`
3. `memory_store`
4. `memory_update`
5. `memory_status`

The public transport is command-launched **stdio**. Owner CLI/WebUI surfaces are separate operator interfaces; they do not add MCP tools.

The live MCP schema and `factlane --help-tools` are authoritative for exact fields, enum values, and signatures.

## `memory_search`

Search bounded facts inside one exact scope.

Key dimensions include:

- exact public scope and required identity;
- intent class from the live schema;
- retrieval lifecycle (`CURRENT` or explicit `REVIEW_HISTORY`);
- search kind (`EXACT`, `KEYWORD`, `SEMANTIC`, or `HYBRID` where allowed).

`CURRENT` retrieval admits only eligible Current records. For semantic search, eligibility is applied inside the vector candidate path before the final KNN result limit so closer unverified Candidates cannot crowd out eligible Current facts.

`CROSS_PROJECT_WORKFLOW` follows its own search matrix and does not fan out across project scopes.

## `memory_get`

Read one logical memory by its governed identity and inspect its provenance, lifecycle, revision, freshness, and related metadata.

Use it when you need the exact record rather than ranked search.

A record can exist in history without being eligible for Current use.

## `memory_store`

Contribute one bounded fact.

A store request needs the exact fields defined by the live schema, including memory type, source provenance, freshness policy, scope, and idempotency key.

Two permissions are separate:

1. **content consent** — the user explicitly asked to remember/store the fact, or explicitly accepted an agent proposal;
2. **launcher authority** — the active process profile permits Candidate contribution.

A normal delegated agent stores a `CANDIDATE`; it does not mint `VALIDATED_CURRENT`.

## `memory_update`

Perform a governed update under exact revision/idempotency checks.

Core modes are:

- `REVERIFY` — verify the same logical memory again; Candidate promotion uses exact `expected_record_id`;
- `REPLACE` — create a governed successor for an eligible current logical memory; it is not a Candidate-edit path;
- `REVOKE` — direct Owner lifecycle disposition for an eligible `VALIDATED_CURRENT` record;
- `REJECT` — direct Owner lifecycle disposition for a `CANDIDATE`.

`REVOKE` and `REJECT` additionally require the exact current `expected_record_id`, the displayed `observed_group_token`, and an allowed bounded reason code. They are logical lifecycle states, not physical erasure.

A delegated agent does not gain verifier or Owner authority because `memory_update` is visible.

## `memory_status`

Return bounded read-only observations about a scope's storage and embedding-profile state.

The status surface can report compatibility/binding state, lifecycle counts, capacity/retention observations, and whether the current runtime fingerprint still matches persisted embedding expectations.

It does not migrate storage, re-embed records, silently requalify a changed runtime, run background maintenance, or act as a general monitoring service.

When compatibility is unproven or mismatched, FactLane preserves safe read paths where the contract allows them and fails closed on semantic/vector-mutating operations that require the incompatible binding.

## Owner operations are separate

`OwnerControlService` exposes trusted local governance beyond the ordinary MCP interaction model:

- inventory and record detail;
- Candidate, Current, history, conflicts, health, and retention views;
- revoke/reject;
- contradiction resolution;
- trusted reverify/replace;
- manual `on_change` freshness recheck;
- readmission of eligible revoked memory.

Use `factlane-owner --help` for the current CLI contract.

Use `factlane-owner-webui --help` for the direct loopback Owner UI launcher. It is distinct from the managed `factlane setup|serve|open|start|stop|status|restart webui` lifecycle. Neither Owner UI surface is an HTTP MCP transport.

In current development, the managed Owner Panel can also expose capability-gated database workflows such as explicit backup artifacts, import/export, full restore, and platform-specific permanent purge. These are Owner/operator workflows rather than MCP tools; see the [Quick Start](QUICKSTART.md) for the user-facing boundary.

## Governed failures

Expected governed failures use the normal MCP result channel with:

- `status=BLOCKED`;
- a stable `error_code`;
- a safe message;
- empty results;
- retry metadata where applicable.

Branch on `error_code`, not exception prose.

Unexpected internal exceptions remain transport errors rather than being relabeled as governed FactLane outcomes.

## Example: verify connectivity before writing

A simple global-user status request:

```json
{"request":{"scope":"GLOBAL_USER"}}
```

A project-scoped Current keyword search may look like:

```json
{
  "request": {
    "scope": "PROJECT",
    "project_id": "example-project",
    "intent_class": "CURRENT_PROJECT_STATE",
    "query": "What release constraints are recorded?",
    "retrieval_mode": "CURRENT",
    "retrieval_mode_kind": "KEYWORD"
  }
}
```

When a trusted host already binds the applicable project/workflow/tool identity, follow the live schema and omit conflicting caller-supplied identity.

## Next

- [Core concepts](CORE_CONCEPTS.md) — lifecycle and authority model.
- [Quick Start](QUICKSTART.md) — safe connection and first verification.
- [Architecture](ARCHITECTURE.md) — request, storage, migration, and Owner-control internals.
