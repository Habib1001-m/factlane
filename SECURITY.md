# Security Policy

FactLane is an early-stage local-first memory infrastructure project. Please report security issues privately to the repository owner rather than opening a public issue containing sensitive material.

Do not include secrets, credentials, raw transcripts, raw user memory, private evidence bundles, or identifying local filesystem data in public bug reports. Use the smallest synthetic reproduction that demonstrates the issue.

## Product security boundaries

- Memory is supporting state, not execution authority.
- The default local embedding provider boundary accepts loopback HTTP only.
- FactLane has no automatic external embedding fallback.
- The normal agent surface is exactly five tools and excludes delete/admin/config/harvest/distill/consolidation operations.
- Transport-bound host identity is accepted at the explicit trusted-launcher/stdio gateway boundary; request-side identity claims are rejected and unsupported runtime transports fail closed.
- Multi-client lost-update prevention is accepted through transaction-local single-winner CAS.
- The accepted 4C-06 crash proof demonstrates atomic rollback, post-commit durability, and idempotent replay at tested boundaries; it is not a recovery service.
- Live Codex/Hermes configuration mutation is outside normal repository test execution and requires an explicit Owner gate.

## Explicit limitations

- Launcher-supplied host binding is not cryptographic or operating-system process attestation.
- FactLane is not a distributed coordination system.
- Retention, reclaim, archive/recovery, and lifecycle work remain later work.
- Native-memory production migration remains later work.
- The production embedding profile remains unresolved.

If a vulnerability could expose memory across scopes, bypass provenance/authority checks, mutate durable state without authorization, leak secrets, or turn memory into execution authority, treat it as high priority.
