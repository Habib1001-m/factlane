# Project history

FactLane grew from a question about reusable agent memory: how can separate tools share a
useful fact without importing each other's transcript, authority, or current project state?
The design answer is a small, scoped fact store with explicit provenance, freshness,
verification, and revision history.

The initial local MCP layer established five bounded memory operations and exact scope
handling. Later storage work added transaction-local compare-and-swap for multi-client
updates, crash-safe revision changes, and visibility into retention and capacity. Eligible
superseded records can be compacted without presenting historical records as current facts.

The current storage contract separates the identity of a contributor from the authority of a
verifier. A normal agent may submit a Candidate; a trusted verifier must promote it. The
gateway can bind project, workflow, and tool identities from a trusted host rather than
accepting conflicting assertions in a request. Semantic `CURRENT` search limits are applied
after validation eligibility so unverified Candidates cannot displace current results.

The project also introduced an operator-only path for sensitive-memory incident recovery,
explicit compatibility checks for the pinned SQLite backend, and a linked SQLite **3.42.0+**
floor. These controls do not enlarge the five-tool MCP surface.

FactLane remains local-first and under continued qualification. The source repository and
package build are available for technical review, but authoritative backup/restore
acceptance, a curated production-corpus evaluation, and broader workload validation are
separate from the implemented features described here.

For present-day behavior rather than the project timeline, start with the
[README](../README.md), [architecture](ARCHITECTURE.md), or [quick start](QUICKSTART.md).
