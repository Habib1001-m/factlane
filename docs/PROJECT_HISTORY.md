# FactLane Project History

FactLane began as the portable S6B.4B implementation pilot under the temporary project identity `one-linux-codex-memory`. The public repository intentionally starts with a clean canonical genesis rather than publishing the private pilot commit history as product history.

```text
Pilot repository: one-linux-codex-memory
Accepted source head: 6ac7f2a4a19c57e4e0e70c85dd055b5de9ad074c
Canonical review archive filename: ONE_LINUX_MEMORY_CANONICAL_REVIEW_CLONE_20260828T225508Z.tar.gz
Canonical review archive SHA-256: b8b61e1a1c1531baa0077eca9e5e1abf97b45bc828e34d9377d1250a6966089b
Custody owner: Project Owner
Archive class: PRIVATE_FORENSIC_EVIDENCE
Public distribution: NO
Integrity check: SHA256 + git fsck
S6B.4B disposition: CLOSED_PASS
Public product identity: FactLane
```

The workstation-specific custody path remains private evidence and is not part of the public product contract.

## Exact dependency/license provenance at genesis

```text
mcp-memory-service pin: e5155b937051db4fa99a384018c5ebd621d8c5ef
mcp-memory-service license at exact pin: Apache-2.0 verified
sqlite-vec resolved release: v0.1.9
sqlite-vec Apache-2.0-compatible license at exact release: verified
LICENSE_VERIFICATION=EXACT_PINNED_REVISIONS_ONLY
```

Current upstream default branches are not substituted for the exact revisions used by the accepted build.


## Canonical genesis pre-acceptance

The exact private implementation candidate `8a768fa66d86451d5f2297979f508c416e035dbb` passed the owner-workstation R2 pre-acceptance gate on 2026-08-29. The private evidence archive is retained outside the public repository; this public history records only its identity and integrity hash.

```text
Evidence archive: FACTLANE_GENESIS_PRE_ACCEPTANCE_EVIDENCE_20260829T042104Z.tar.gz
Evidence SHA-256: 6311ff329deb7eda01d8b7e48eaf252c48ddae381de87b4ff765a640be8f2dd5
Internal SHA256SUMS: PASS
uv sync --frozen --dev: PASS
Full pytest: 22 PASS
Exact backend pin runtime: PASS
Nomic exact digest runtime: PASS
Nomic effective context window: 2048
Bounded 2000-byte fact with truncate=false: PASS
Bounded 512-byte query with truncate=false: PASS
Hermes runtime dependencies: 0
```

This acceptance did not authorize or start S6B.4C and did not mutate live Codex/Hermes configuration, real memory, legacy data, or Knowledge.

## S6B.4C shared-store concurrency campaign

The completed S6B.4C campaign extended the accepted FactLane baseline in six
bounded slices: 4C-01 established the execution-context preflight and harness;
4C-02 accepted transport-bound host identity and the shared gateway; 4C-03
accepted transaction-local atomic CAS and lost-update prevention; 4C-04 accepted
disposable process concurrency plus real Codex/Hermes execution-context evidence;
4C-05 accepted async embedding concurrency and real pinned-backend/local-provider
runtime proof; and 4C-06 accepted disposable-process SIGKILL crash atomicity,
post-commit durability/idempotent replay, and async cancellation characterization.

```text
S6B_4C=CLOSED_PASS
FINAL_4C_IMPLEMENTATION_PR=14
PR14_MERGE_SHA=e521ab48b7948785acc35b1e01d75db36a0a4088
FINAL_4C_CLOSURE_PR=15
PR15_MERGE_SHA=fae180954f6add312d4a53d8bd2e9f266e7e190c
PR15_POST_MERGE_CI_RUN=33347562429
PR15_POST_MERGE_CI=PASS
FULL_4C06_ACCEPTANCE_TESTS=90_PASS
```

No S6B.4D implementation was started, and no S6B.5/native-memory migration was
started. Production embedding-profile selection remains undecided.
