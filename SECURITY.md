# Security Policy

FactLane is local-first memory infrastructure. Please report security issues
privately to the repository owner rather than opening a public issue containing
sensitive material.

Do not include secrets, credentials, raw transcripts, raw user memory, private
evidence bundles, or identifying local filesystem data in public bug reports. Use the
smallest synthetic reproduction that demonstrates the issue.

## Product security boundaries

- Memory is supporting state, not execution authority.
- The local embedding provider accepts loopback HTTP only and has no external fallback.
- The normal agent surface is exactly five tools and excludes delete, administration,
  configuration mutation, harvesting, distillation, and consolidation operations.
- Host identity is bound at the trusted launcher/stdio gateway boundary; request-side
  identity claims are rejected and unsupported transports fail closed.
- Multi-client lost-update prevention uses transaction-local single-winner CAS.
- Transaction boundaries provide atomic rollback, post-commit durability, and
  idempotent replay for the supported operations.

## Explicit limitations

- Launcher-supplied host binding is not cryptographic or operating-system process
  attestation.
- FactLane is not a distributed coordination system.
- Retention, reclaim, archive/recovery, and lifecycle work remain future work.
- Native-memory production migration remains future work.
- The production embedding profile remains unresolved.

If a vulnerability could expose memory across scopes, bypass provenance or authority
checks, mutate durable state without authorization, leak secrets, or turn memory into
execution authority, treat it as high priority.
