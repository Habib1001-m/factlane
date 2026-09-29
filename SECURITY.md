# Security Policy

FactLane is local-first memory infrastructure. Please report security issues privately
to the repository owner rather than opening a public issue containing sensitive
material.

Do not include secrets, credentials, raw transcripts, raw user memory, private evidence
bundles, or identifying local filesystem data in public bug reports. Use the smallest
synthetic reproduction that demonstrates the issue.

## Product security boundaries

- Memory is supporting state, not execution authority.
- The local embedding provider accepts loopback HTTP only and has no external fallback.
- The selected production embedding profile is exact-digest pinned and runtime identity,
  capability, native dimension, and input-size mismatches fail closed.
- The normal agent surface is exactly five tools and excludes delete, administration,
  configuration mutation, harvesting, distillation, and consolidation operations.
- Host identity is bound at the trusted launcher/stdio gateway boundary; request-side
  identity claims are rejected and unsupported transports fail closed.
- Multi-client lost-update prevention uses transaction-local single-winner CAS.
- Transaction boundaries provide atomic rollback, post-commit durability, and
  idempotent replay for the supported operations.
- Retention/capacity observations are read-only; bounded manual housekeeping preserves
  current authority and reuses the accepted atomic compaction path.

## Sensitive-memory incident recovery

- Sensitive-memory recovery is a trusted-operator-only local maintenance surface in
  `factlane.recovery`, outside FastMCP, `MemoryGateway`, `public_contract`, and
  `PUBLIC_TOOL_NAMES`. It does not add a public delete, administration, or recovery tool;
  `memory_update` and normal compaction are not purge mechanisms.
- Recovery is bound to an explicit frozen target set and exact database/profile state.
  Maintenance quiescence, an exclusive recovery lease, known schema/materialization,
  exact target binding, complete propagation, and the unchanged five-tool public contract
  are hard fail-closed preconditions; if they cannot be proven, recovery does not mutate.
- For a confirmed sensitive-memory incident, the logical purge is atomic. A pre-commit
  failure rolls back the operation. A post-commit sealing/promotion failure persists
  `S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE`; normal service restart and restoration
  of the sensitive payload are forbidden until safe idempotent sealing reaches
  `S1_LOCAL_FACTLANE_PURGE_VERIFIED`.
- S1 recovery does not create a plaintext pre-mutation backup by default. Any forensic
  snapshot requires separate explicit authority and containment, and external copies or
  credential incidents require separate handling; local purge is not a claim of universal
  or hardware-level erasure.
- Availability of this operator does not authorize production recovery. Running it against
  a live/production database remains a separately authorized incident action.

## Explicit limitations

- Launcher-supplied host binding is not cryptographic or operating-system process
  attestation.
- FactLane is not a distributed coordination system.
- Housekeeping is not an automatic background retention service, backup system, or
  disaster-recovery subsystem.
- Controlled Codex/Hermes real-host qualification is accepted evidence for the qualified
  post-R2 identity, but it does not by itself make this checkout/package a deployed or
  publicly released security baseline. Authoritative backup/restore acceptance is not yet
  part of the final production-grade claim.
- Retrieval specificity under Arabic/mixed-language and document-crowding cases remains
  a known quality limitation; it is not treated as an authority or scope bypass.

If a vulnerability could expose memory across scopes, bypass provenance or authority
checks, mutate durable state without authorization, leak secrets, or turn memory into
execution authority, treat it as high priority.
