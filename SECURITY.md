# Security Policy

FactLane is an early-stage local-first memory infrastructure project. Please report security issues privately to the repository owner rather than opening a public issue containing sensitive material.

Do not include secrets, credentials, raw transcripts, raw user memory, private evidence bundles, or identifying local filesystem data in public bug reports. Use the smallest synthetic reproduction that demonstrates the issue.

## Product security boundaries

- Memory is supporting state, not execution authority.
- The default local embedding provider boundary accepts loopback HTTP only.
- FactLane has no automatic external embedding fallback.
- The normal agent surface is exactly five tools and excludes delete/admin/config/harvest/distill/consolidation operations.
- Host identity and multi-client write coordination are not claimed production-ready until their explicit later gates pass.
- Live Codex/Hermes configuration mutation is outside normal repository test execution and requires an explicit Owner gate.

If a vulnerability could expose memory across scopes, bypass provenance/authority checks, mutate durable state without authorization, leak secrets, or turn memory into execution authority, treat it as high priority.
