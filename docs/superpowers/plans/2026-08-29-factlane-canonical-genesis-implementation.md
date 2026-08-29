# FactLane Canonical Public Genesis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the accepted S6B.4B `one-linux-codex-memory` pilot at `6ac7f2a4a19c57e4e0e70c85dd055b5de9ad074c` into one verified, public, host-neutral FactLane canonical genesis commit without starting S6B.4C or mutating live Codex/Hermes configuration.

**Architecture:** Preserve the accepted five-tool Router → narrow adapter → SQLite-vec boundary while changing product identity from Codex-specific bootstrap naming to FactLane. Harden only the defects/debts explicitly admitted by the amended genesis spec: exactly-once Nomic task prefix regression coverage, fail-closed embedding overflow, explicit backend-compatibility assumptions/reuse, repository-native governance, provenance, public hygiene, and clean parentless public history. Multi-host identity binding, multi-client revision/CAS, crash injection, retention/compaction, and production profile selection remain later gates.

**Tech Stack:** Python 3.11+, `uv`, `pytest`, MCP/FastMCP 1.29.1, pinned `doobidoo/mcp-memory-service` commit `e5155b937051db4fa99a384018c5ebd621d8c5ef`, `sqlite-vec==0.1.9`, local Ollama embedding API, Git/GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-29-factlane-canonical-genesis-design.md`

## Global Constraints

- Accepted source pilot head is exactly `6ac7f2a4a19c57e4e0e70c85dd055b5de9ad074c`.
- Public target is exactly `Habib1001-m/factlane` and must still be empty before the public genesis push.
- Product/package/CLI/MCP identity is `FactLane` / `factlane` / `factlane` / `factlane`.
- Normal agent-facing MCP tool surface remains exactly five operations: `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status`.
- Backend pin remains exactly `e5155b937051db4fa99a384018c5ebd621d8c5ef`; `sqlite-vec` remains `0.1.9` in this slice.
- Baseline remains local-first, CPU-capable, no Docker/GPU/cloud LLM/external embedding API requirement, no persistent service requirement.
- Nomic document/query task prefixes are `search_document: ` and `search_query: ` and must be applied exactly once by the provider boundary.
- Embedding overflow must fail closed; normal runtime must not request silent truncation.
- `uv.lock` owns Python dependency resolution; `environment-provenance.json` owns only important external runtime/build identities outside the lock.
- Reuse pinned-backend SQLite locking/retry/WAL/busy-timeout primitives before adding any new concurrency mechanism.
- No live Codex config mutation, Hermes config/runtime mutation, global MCP registration, real user-memory bootstrap, legacy import, Knowledge mutation, or S6B.4C execution.
- The canonical project taskboard is one file, `TASKBOARD.md`, updated in place. Never create `TASKBOARD_V2.md`, `TASKBOARD_V3.md`, etc.
- Public history must contain one parentless canonical genesis commit. Pilot/development commits remain private forensic provenance.
- Public push is blocked by any semantic regression of accepted S6B.4B behavior or any failed acceptance criterion in the amended spec.

---

## File Structure Locked by This Plan

### Product runtime

- Create by rename: `src/factlane/__init__.py` — public Python package surface.
- Create by rename: `src/factlane/contract.py` — scope/freshness/security envelope contract.
- Create by rename: `src/factlane/router.py` — Truth Router decision boundary.
- Create by rename + hardening: `src/factlane/embeddings.py` — local provider boundary, exact prefixes, fail-closed overflow, verified context metadata.
- Create by rename + compatibility cleanup: `src/factlane/storage.py` — adapter-owned schema/query mapping over pinned backend; no duplicate lock/retry implementation.
- Create by rename: `src/factlane/adapter.py` — exactly five logical memory operations and lineage/budget policy.
- Create by rename: `src/factlane/server.py` — FactLane MCP stdio server and CLI entry point.

### Test/evidence surface

- Create: `tests/unit/test_contract.py` — existing six contract tests, renamed imports, plus lightweight product identity checks where appropriate.
- Create: `tests/unit/test_embeddings.py` — exact-prefix, truncate-false, context metadata, remote URL, malformed provider response tests.
- Create: `tests/integration/test_storage_backend.py` — pinned backend compatibility and SQLite pragma/retry delegation assertions on a disposable DB.
- Create: `tests/acceptance/s6b4b_pilot.py` — retained opt-in S6B.4B evidence harness moved out of production runtime, with the Matryoshka double-prefix fixture corrected.
- Delete from runtime: `src/one_linux_codex_memory/pilot.py` after its reusable content has moved.
- Delete tracked generated tree: `src/one_linux_codex_memory.egg-info/`.

### Repository governance/public surface

- Create: `AGENTS.md` — short agent bootstrap and authority/read-order/stop rules.
- Create: `TASKBOARD.md` — single canonical in-repo project board.
- Replace: `README.md` — public FactLane contract and development quick start.
- Create: `LICENSE` — Apache License 2.0 text.
- Create: `SECURITY.md` — responsible reporting + local-only/security boundaries.
- Create: `environment-provenance.json` — external runtime/build provenance only.
- Create: `docs/ARCHITECTURE.md` — Router/adapter/backend/provider ownership map.
- Create: `docs/GOVERNANCE.md` — Git/taskboard/evidence/reuse workflow.
- Create: `docs/ENVIRONMENT.md` — isolated runtime and REUSE_FIRST/PROVENANCE_ALWAYS rules.
- Create: `docs/PROJECT_HISTORY.md` — pilot head/archive/custody/license transition record.
- Keep: `docs/superpowers/specs/2026-08-29-factlane-canonical-genesis-design.md`.
- Keep: `docs/superpowers/plans/2026-08-29-factlane-canonical-genesis-implementation.md`.
- Create: `.github/workflows/ci.yml` — one Python 3.11 CI baseline only.
- Modify: `.gitignore` — ignore `.venv/`, caches, `*.egg-info/`, local DB/evidence/runtime output.
- Modify: `pyproject.toml` — package/CLI identity `factlane`; dependencies otherwise unchanged.
- Modify deterministically: `uv.lock` — root project identity only unless resolution proves another necessary lock change.

---

### Task 1: Establish an Isolated, Traceable Implementation Worktree

**Files:**
- Read: source repository at accepted head `6ac7f2a4a19c57e4e0e70c85dd055b5de9ad074c`
- Add to private implementation branch: `docs/superpowers/specs/2026-08-29-factlane-canonical-genesis-design.md`
- Add to private implementation branch: `docs/superpowers/plans/2026-08-29-factlane-canonical-genesis-implementation.md`

**Interfaces:**
- Consumes: the canonical review clone and accepted amended spec/plan bytes.
- Produces: private branch `factlane-canonical-genesis-implementation` with source identity proven before product edits.

- [ ] **Step 1: Verify the source repository before creating a worktree**

Run from the canonical review clone:

```bash
SOURCE_HEAD=6ac7f2a4a19c57e4e0e70c85dd055b5de9ad074c

test "$(git rev-parse HEAD)" = "$SOURCE_HEAD"
test -z "$(git status --porcelain)"
git fsck --full
```

Expected: all commands exit `0`; `git fsck --full` emits no integrity error.

- [ ] **Step 2: Verify the public GitHub target is still empty**

Run:

```bash
git ls-remote https://github.com/Habib1001-m/factlane.git
```

Expected: no branch/tag refs. If any ref exists, stop with `PUBLIC_TARGET_NOT_EMPTY=HOLD`; do not force-push or rewrite it.

- [ ] **Step 3: Create the isolated implementation worktree**

Use the `superpowers:using-git-worktrees` workflow. The resulting worktree must be on a new local branch named:

```text
factlane-canonical-genesis-implementation
```

Expected: source clone remains clean and unchanged.

- [ ] **Step 4: Copy only the approved spec and this plan into the worktree**

Create:

```text
docs/superpowers/specs/2026-08-29-factlane-canonical-genesis-design.md
docs/superpowers/plans/2026-08-29-factlane-canonical-genesis-implementation.md
```

Do not copy external taskboard archives, review bundles, user memory, or private evidence into the repository.

- [ ] **Step 5: Verify document identities and commit the planning baseline**

Run:

```bash
sha256sum \
  docs/superpowers/specs/2026-08-29-factlane-canonical-genesis-design.md \
  docs/superpowers/plans/2026-08-29-factlane-canonical-genesis-implementation.md

git diff --check
git add docs/superpowers/specs docs/superpowers/plans
git commit -m "docs: lock FactLane canonical genesis plan"
```

Expected: clean commit containing only the approved design and implementation plan.

---

### Task 2: Rename the Product Surface to FactLane Without Semantic Change

**Files:**
- Move: `src/one_linux_codex_memory/` → `src/factlane/`
- Modify: `src/factlane/__init__.py`
- Modify: `src/factlane/server.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify/Create: `tests/unit/test_contract.py`
- Delete: old `tests/test_contract.py` after migration.

**Interfaces:**
- Consumes: current classes `MemoryAdapter`, `AdapterError`, `ScopeContext`, `EmbeddingProfile`, `EmbeddingProvider`, `OllamaLocalProvider`, `TruthRouter`.
- Produces: import surface `factlane.*`, CLI `factlane`, MCP server name `factlane`, unchanged five logical tool names.

- [ ] **Step 1: Write failing product-identity tests before renaming**

Create `tests/unit/test_contract.py` from the existing six tests, but import from `factlane` and add:

```python
from factlane.adapter import MemoryAdapter
from factlane.server import build_mcp_server


def test_normal_agent_surface_is_exactly_five() -> None:
    assert MemoryAdapter.tool_names() == [
        "memory_search",
        "memory_get",
        "memory_store",
        "memory_update",
        "memory_status",
    ]
```

Keep all existing scope/freshness/router/provider tests with only import-path changes.

- [ ] **Step 2: Run the renamed unit test module and verify red state**

Run:

```bash
uv run pytest tests/unit/test_contract.py -q
```

Expected: collection/import failure because package `factlane` does not exist yet.

- [ ] **Step 3: Rename the package directory and public package text**

Run:

```bash
git mv src/one_linux_codex_memory src/factlane
```

Set `src/factlane/__init__.py` first line to:

```python
"""FactLane governed memory plane."""
```

Do not rename technically precise internal symbols such as `TruthRouter` solely for branding.

- [ ] **Step 4: Change project metadata and CLI identity**

In `pyproject.toml`, set exactly:

```toml
[project]
name = "factlane"
version = "0.1.0"
description = "Local-first, multi-host governed memory plane for AI agents"
requires-python = ">=3.11"

[project.scripts]
factlane = "factlane.server:main"
```

Keep the existing dependency pins unchanged.

- [ ] **Step 5: Change only the MCP server product identity**

In `src/factlane/server.py`, change:

```python
FastMCP(
    "one-linux-codex-memory",
```

to:

```python
FastMCP(
    "factlane",
```

Keep stdio runtime and the exact five tool names unchanged. In `MemoryAdapter.create()`, change the bootstrap-only default runtime label from `"one-linux-codex-pilot"` to the host-neutral `"factlane-local"`; this is naming cleanup only and does **not** close the later host-identity-binding gate.

- [ ] **Step 6: Regenerate the root lock identity without dependency drift**

First run:

```bash
uv lock --offline
```

Then inspect:

```bash
grep -n 'name = "factlane"' uv.lock
grep -n 'one-linux-codex-memory' uv.lock && exit 1 || true
```

If offline lock refresh is impossible solely because required resolver metadata is missing, a normal `uv lock` network fetch is justified; record that acquisition in execution evidence. Do not upgrade package versions or backend revisions in this task.

- [ ] **Step 7: Run unit tests and package/CLI smoke**

Run:

```bash
uv sync --frozen --dev
uv run pytest tests/unit/test_contract.py -q
uv run python -c 'import factlane; print(factlane.__name__)'
uv run factlane --help
```

Expected: tests pass, import prints `factlane`, CLI help exits `0`.

- [ ] **Step 8: Prove old public identity references are gone from runtime/package metadata**

Run:

```bash
if grep -RIn --exclude-dir=.git --exclude='*.md' \
  -E 'one-linux-codex-memory|one_linux_codex_memory|One Linux Codex' \
  pyproject.toml uv.lock src tests; then
  exit 1
fi
```

Expected: no matches.

- [ ] **Step 9: Commit the identity migration**

```bash
git add -A
git diff --check
git commit -m "refactor: establish FactLane product identity"
```

---

### Task 3: Remove Generated Runtime Noise and Move the Pilot Harness Out of the Product Package

**Files:**
- Delete: `src/one_linux_codex_memory.egg-info/` or renamed equivalent if still present.
- Move: `src/factlane/pilot.py` → `tests/acceptance/s6b4b_pilot.py`.
- Modify: `tests/acceptance/s6b4b_pilot.py` imports and module invocation.
- Modify: `.gitignore`.

**Interfaces:**
- Consumes: S6B.4B pilot helper functions and acceptance evidence logic.
- Produces: production package with no pilot harness; opt-in acceptance harness preserved outside runtime installation.

- [ ] **Step 1: Add a failing source-hygiene assertion**

Create `tests/unit/test_repository_contract.py`:

```python
from pathlib import Path


def test_production_package_contains_no_pilot_module() -> None:
    assert not Path("src/factlane/pilot.py").exists()


def test_tracked_generated_egg_info_is_absent() -> None:
    assert not list(Path("src").glob("*.egg-info"))
```

- [ ] **Step 2: Run and verify the hygiene test fails before the move**

```bash
uv run pytest tests/unit/test_repository_contract.py -q
```

Expected: failure because `src/factlane/pilot.py` and tracked egg-info still exist.

- [ ] **Step 3: Move the pilot harness into acceptance tests**

Run:

```bash
mkdir -p tests/acceptance
git mv src/factlane/pilot.py tests/acceptance/s6b4b_pilot.py
```

Change relative imports:

```python
from factlane.adapter import MemoryAdapter, PROFILE_DEFINITIONS, TokenCounter
from factlane.contract import AdapterError, canonical_json, iso_now
from factlane.embeddings import OllamaLocalProvider
```

Change the MCP wire subprocess module from:

```python
"one_linux_codex_memory.server"
```

to:

```python
"factlane.server"
```

- [ ] **Step 4: Correct the known Matryoshka fixture double-prefix wart**

In `verify_models`, pass an **unprefixed** fixture string to `embed_documents()` so the provider owns the one and only `search_document: ` prefix. Add an inline assertion/test helper that the captured `/api/embed` input begins with exactly one prefix, not two.

- [ ] **Step 5: Remove generated egg-info and expand `.gitignore`**

Run:

```bash
git rm -r src/one_linux_codex_memory.egg-info 2>/dev/null || true
git rm -r src/factlane.egg-info 2>/dev/null || true
```

Ensure `.gitignore` contains:

```gitignore
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
.mypy_cache/
*.pyc
*.egg-info/
*.db
*.sqlite
*.sqlite3
pilot-evidence/
```

- [ ] **Step 6: Re-run hygiene and unit tests**

```bash
uv run pytest tests/unit -q
```

Expected: hygiene test and renamed contract tests pass.

- [ ] **Step 7: Verify the acceptance harness is not packaged**

Build an sdist/wheel into a disposable directory:

```bash
rm -rf /tmp/factlane-dist-check
mkdir -p /tmp/factlane-dist-check
uv build --out-dir /tmp/factlane-dist-check
python3 - <<'PY'
from pathlib import Path
import zipfile
wheel = next(Path('/tmp/factlane-dist-check').glob('*.whl'))
with zipfile.ZipFile(wheel) as zf:
    names = zf.namelist()
assert not any('s6b4b_pilot' in name for name in names), names
assert any(name.startswith('factlane/') for name in names), names
PY
```

Expected: wheel includes `factlane/`, no acceptance pilot.

- [ ] **Step 8: Commit runtime-surface cleanup**

```bash
git add -A
git diff --check
git commit -m "test: move pilot evidence harness out of runtime"
```

---

### Task 4: Harden the Embedding Provider Contract Without Reopening the Profile Decision

**Files:**
- Modify: `src/factlane/embeddings.py`
- Modify: `src/factlane/adapter.py` only if profile construction needs a new explicit field.
- Create: `tests/unit/test_embeddings.py`
- Modify: `tests/acceptance/s6b4b_pilot.py` only for live verification hooks.

**Interfaces:**
- Consumes: `EmbeddingProfile`, `OllamaLocalProvider`, Nomic prefixes in `PROFILE_DEFINITIONS`.
- Produces: exact-once provider-owned prefixing, `truncate=False`, explicit provider context metadata, fail-closed provider error handling, unchanged vector dimensions and cosine-normalization checks.

- [ ] **Step 1: Write a recording provider test double around `_request`**

In `tests/unit/test_embeddings.py`, define:

```python
from factlane.embeddings import OllamaLocalProvider


class RecordingProvider(OllamaLocalProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests: list[tuple[str, dict[str, object] | None]] = []

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.requests.append((path, payload))
        if path == "/api/embed":
            dimensions = int(payload["dimensions"])
            count = len(payload["input"])
            unit = [1.0] + [0.0] * (dimensions - 1)
            return {"embeddings": [unit[:] for _ in range(count)]}
        raise AssertionError(path)
```

Use the pinned Nomic digest and `source_dimension=768` in tests.

- [ ] **Step 2: Write failing exactly-once document/query prefix tests**

Add:

```python
def test_nomic_prefixes_are_applied_exactly_once() -> None:
    provider = RecordingProvider(
        model="nomic-embed-text:latest",
        profile_id="nomic-256",
        output_dimension=256,
        source_dimension=768,
        model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
        document_prefix="search_document: ",
        query_prefix="search_query: ",
    )
    provider.embed_documents(["alpha"])
    provider.embed_query("beta")
    assert provider.requests[0][1]["input"] == ["search_document: alpha"]
    assert provider.requests[1][1]["input"] == ["search_query: beta"]
```

- [ ] **Step 3: Write a failing no-silent-truncation test**

Add:

```python
def test_embed_requests_disable_provider_truncation() -> None:
    provider = RecordingProvider(
        model="nomic-embed-text:latest",
        profile_id="nomic-256",
        output_dimension=256,
        source_dimension=768,
        model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
        document_prefix="search_document: ",
        query_prefix="search_query: ",
    )
    provider.embed_documents(["alpha"])
    assert provider.requests[0][1]["truncate"] is False
```

Expected red state: current implementation sends `True`.

- [ ] **Step 4: Implement provider-owned prefixing + `truncate=False` only**

In `OllamaLocalProvider._embed`, keep:

```python
"input": [prefix + text for text in texts],
```

and change only:

```python
"truncate": False,
```

Do not add a global automatic de-prefix/re-prefix rewrite of user facts. The contract is: caller gives raw fact/query text; provider applies its profile prefix exactly once.

- [ ] **Step 5: Add deterministic context-metadata extraction**

Add a helper in `embeddings.py`:

```python
def _context_length_from_model_info(model_info: dict[str, object]) -> int | None:
    preferred = (
        "nomic-bert.context_length",
        "bert.context_length",
        "nomic-bert.max_position_embeddings",
        "bert.max_position_embeddings",
    )
    for key in preferred:
        value = model_info.get(key)
        if isinstance(value, int) and value > 0:
            return value
    for key, value in model_info.items():
        if (
            isinstance(key, str)
            and key.endswith((".context_length", ".max_position_embeddings"))
            and isinstance(value, int)
            and value > 0
        ):
            return value
    return None
```

Expose the result from `provider_status()` as:

```python
"effective_context_window": _context_length_from_model_info(model_info),
"truncate_policy": "FAIL_CLOSED_PROVIDER_REJECTION",
```

A missing context field is allowed in normal status only as `None`; it must not be fabricated as 8192.

- [ ] **Step 6: Add unit tests for context metadata and provider rejection**

Test `_context_length_from_model_info` with explicit `nomic-bert.context_length`, fallback suffix match, and missing key returning `None`.

Also test that when `/api/embed` returns:

```json
{"error":"input length exceeds context length"}
```

`OllamaLocalProvider` raises `AdapterError` with code `EMBEDDING_UNAVAILABLE` and does not retry with truncation enabled.

- [ ] **Step 7: Run embedding tests**

```bash
uv run pytest tests/unit/test_embeddings.py -q
```

Expected: all embedding provider tests pass.

- [ ] **Step 8: Run opt-in live bounded-input probes against the exact local Nomic artifact**

Use the acceptance harness to call the exact pinned digest with `truncate=False` for:

```text
FACT_MAX_BYTES=2000
QUERY_MAX_BYTES=512
```

Use valid UTF-8 synthetic strings exactly at those byte ceilings and the approved prefixes. Record whether both provider calls succeed without truncation. This is acceptance evidence, not a normal CI dependency.

If either bounded input is rejected, stop public genesis with `EMBEDDING_BOUNDED_INPUT_ACCEPTANCE=HOLD`; do not silently reduce the FactLane contract or force `num_ctx=8192`.

- [ ] **Step 9: Commit embedding hardening**

```bash
git add src/factlane/embeddings.py src/factlane/adapter.py tests/unit/test_embeddings.py tests/acceptance/s6b4b_pilot.py
git diff --check
git commit -m "test: harden local embedding contract"
```

---

### Task 5: Make the Backend Compatibility Boundary Explicit and Reuse Upstream SQLite Mechanics

**Files:**
- Modify: `src/factlane/storage.py`
- Create: `tests/integration/test_storage_backend.py`
- Document later in Task 6: `docs/ARCHITECTURE.md`, `docs/ENVIRONMENT.md`.

**Interfaces:**
- Consumes: pinned backend private method `SqliteVecMemoryStorage._execute_with_retry`, backend `_conn_lock`, backend-created SQLite connection configured with WAL/busy_timeout.
- Produces: one FactLane wrapper around upstream DB execution/retry; FactLane continues to own adapter schema, scope filters, lineage, idempotency, embedding-profile rows, and transactions.

- [ ] **Step 1: Write a compatibility test proving exact pinned backend primitives exist**

In `tests/integration/test_storage_backend.py`:

```python
from mcp_memory_service.storage.sqlite_vec import SqliteVecMemoryStorage


def test_pinned_backend_exposes_required_sqlite_primitives(tmp_path) -> None:
    storage = SqliteVecMemoryStorage(str(tmp_path / "memory.db"))
    assert hasattr(storage, "_conn_lock")
    assert callable(storage._execute_with_retry)
```

This is intentionally an exact-pin compatibility test. If upstream changes, the pin/change must be consciously reconciled.

- [ ] **Step 2: Write a failing test that FactLane delegates DB execution to backend retry**

Use a fake storage object whose `_execute_with_retry` records the callable and returns its result. Inject it into an opened-like `SQLiteVecEngine` instance and assert `_run` calls `_execute_with_retry` instead of implementing a second lock/backoff loop.

Expected red state: current `_run` manually acquires `_conn_lock`, calls `asyncio.to_thread`, and repeats `locked/busy` retry itself.

- [ ] **Step 3: Replace duplicate `_run` lock/retry code with one compatibility delegation**

Implement `_run` as:

```python
async def _run(self, operation: Callable[..., Any], *args: Any) -> Any:
    if self.conn is None or self.storage is None:
        raise AdapterError("BACKEND_UNAVAILABLE", "backend is not open")

    execute_with_retry = getattr(self.storage, "_execute_with_retry", None)
    if not callable(execute_with_retry):
        raise AdapterError(
            "SCHEMA_MISMATCH",
            "pinned backend no longer exposes the required SQLite retry boundary",
        )

    try:
        return await execute_with_retry(lambda: operation(*args))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise AdapterError("BACKEND_BUSY", "backend remained busy after bounded retry") from exc
        raise
```

Do not remove FactLane's `BEGIN IMMEDIATE` transaction in `write_record`; transaction semantics remain FactLane-owned.

- [ ] **Step 4: Add live disposable-DB pragma assertions**

After `SQLiteVecEngine.open()` on a disposable test DB, assert:

```python
assert engine.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
assert engine.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
```

Also assert adapter schema tables exist and the configured embedding dimension matches the profile.

- [ ] **Step 5: Preserve the existing revision API without claiming multi-client atomicity**

The S6B.4B adapter already exposes `expected_revision` and sequentially raises `VERSION_CONFLICT` when the observed revision does not match. Add this regression assertion to `tests/unit/test_repository_contract.py`:

```python
import inspect

from factlane.adapter import MemoryAdapter


def test_update_api_preserves_expected_revision_contract() -> None:
    parameters = inspect.signature(MemoryAdapter.update).parameters
    assert "expected_revision" in parameters
```

Document in `TASKBOARD.md`/`docs/ARCHITECTURE.md` that this existing API is **not** evidence of atomic multi-client single-winner CAS: the current read/check/write sequence has not yet been proven under concurrent independent clients/processes. S6B.4C owns that proof/remediation.

- [ ] **Step 6: Run integration tests and existing unit tests**

```bash
uv run pytest tests/integration/test_storage_backend.py tests/unit -q
```

Expected: PASS using only disposable local DB state and test doubles; no external network.

- [ ] **Step 7: Commit backend compatibility cleanup**

```bash
git add src/factlane/storage.py tests/integration/test_storage_backend.py
git diff --check
git commit -m "refactor: reuse pinned backend sqlite coordination"
```

---

### Task 6: Establish Repo-Native Governance, Public Documentation, License, and Provenance

**Files:**
- Create: `AGENTS.md`
- Create: `TASKBOARD.md`
- Replace: `README.md`
- Create: `LICENSE`
- Create: `SECURITY.md`
- Create: `environment-provenance.json`
- Create: `docs/ARCHITECTURE.md`
- Create: `docs/GOVERNANCE.md`
- Create: `docs/ENVIRONMENT.md`
- Create: `docs/PROJECT_HISTORY.md`

**Interfaces:**
- Consumes: amended genesis spec, accepted S6B.4B identities, exact-pin review findings.
- Produces: one public repository truth model with no dependency on external numbered taskboard files.

- [ ] **Step 1: Create `AGENTS.md` as the short repository boot file**

Required read order:

```text
1. AGENTS.md
2. TASKBOARD.md
3. active approved spec/plan named by TASKBOARD.md
4. only the code/docs relevant to the active task
```

Required rules include:

```text
MEMORY_IS_SUPPORTING_STATE_NOT_EXECUTION_AUTHORITY
REUSE_FIRST
VERIFY_BEFORE_REUSE
PROVENANCE_ALWAYS
NO_UNBOUNDED_TOOL_HUNTING_AFTER_BOOTSTRAP
NO_SILENT_SCOPE_EXPANSION
NO_LIVE_HOST_MUTATION_WITHOUT_OWNER_GATE
```

Keep architecture/history out of this boot file; link to `docs/ARCHITECTURE.md` and `docs/GOVERNANCE.md` instead.

- [ ] **Step 2: Create the single canonical `TASKBOARD.md`**

The file must contain an internal version field, not a versioned filename:

```text
TASKBOARD_VERSION=1
TASKBOARD_UPDATE_MODE=IN_PLACE_APPEND_AND_RECONCILE
```

Initial frontier:

```text
S6B_4B=CLOSED_PASS
FACTLANE_REPOSITORY_CANONICALIZATION_AND_GOVERNANCE=ACTIVE
S6B_4C=BLOCKED_PENDING_FACTLANE_CANONICAL_GENESIS
S6B_4D=BLOCKED
S6B_5=BLOCKED
S6C_STARTED=NO
```

Record the four pre-4C debts exactly:

```text
P0 HOST_IDENTITY_BINDING
P0 MULTI_CLIENT_WRITE_COORDINATION
P1 ASYNC_EMBEDDING_CONCURRENCY
P1 BACKEND_COMPATIBILITY_BOUNDARY
```

Mark only the backend primitive **discovery/reuse audit** as addressed by genesis; do not mark the full pre-4C compatibility gate closed until the final Task 8 review reconciles all acceptance evidence.

- [ ] **Step 3: Replace README with the truthful FactLane public contract**

README must state:

```text
FactLane = local-first, multi-host governed memory plane for AI agents
SHARE FACTS, NOT CONTEXT
```

Describe the five tools, local CPU baseline, pinned backend, local embedding boundary, supporting-state semantics, current status (`S6B.4B accepted; S6B.4C not accepted`), and development commands:

```bash
uv sync --frozen --dev
uv run pytest
uv run factlane --help
```

Do not claim shared Codex/Hermes concurrency is production-ready.

- [ ] **Step 4: Create architecture/governance/environment docs**

`docs/ARCHITECTURE.md` must explicitly split ownership:

```text
FactLane owns:
- exact scope/freshness/authority policy
- contradiction handling
- logical lineage/idempotency
- five-tool envelope and budgets
- embedding profile identity
- adapter-owned FactLane schema/transactions

Pinned backend owns/reuses:
- SQLite-vec extension/schema primitives
- WAL/busy_timeout initialization
- connection lock/thread offload
- SQLite locked/busy retry mechanics

Later S6B.4C owns:
- transport-bound host identity
- multi-client revision/CAS and lost-update semantics
- shared gateway/write-plane behavior
```

`docs/GOVERNANCE.md` must define:

```text
repo truth → TASKBOARD.md → task branch → implementation → tests/evidence
→ review → commit → TASKBOARD/docs reconciliation → merge
```

and the single-board update rule.

`docs/ENVIRONMENT.md` must state that Hermes may operate the environment but FactLane cannot import/use Hermes Python/site-packages/config/caches as runtime authority.

- [ ] **Step 5: Create `docs/PROJECT_HISTORY.md` with private archive custody metadata**

Include exactly:

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

Do not publish the private absolute workstation custody path.

- [ ] **Step 6: Create Apache-2.0 `LICENSE` and exact-pin attribution text**

Use the standard Apache License 2.0 text. In `docs/PROJECT_HISTORY.md` or `README.md`, record:

```text
mcp-memory-service pin: e5155b937051db4fa99a384018c5ebd621d8c5ef — Apache-2.0 verified at that exact commit
sqlite-vec resolved release: v0.1.9 — Apache-2.0-compatible license verified at that exact release
```

Do not describe current upstream `main` as the license authority.

- [ ] **Step 7: Create `SECURITY.md`**

Document responsible reporting and these boundaries:

```text
- no secrets/raw transcripts/raw user memory in bug reports
- local provider URL is loopback-only by default
- no automatic external embedding fallback
- five normal agent tools do not expose delete/admin/config/harvest/distill/consolidation
- memory is supporting state, not execution authority
```

- [ ] **Step 8: Generate `environment-provenance.json` from actual execution readback**

Use a small script during execution that captures actual external runtime identities. The file schema is:

```json
{
  "schema_version": 1,
  "policy": "REUSE_FIRST_VERIFY_BEFORE_REUSE_PROVENANCE_ALWAYS",
  "assets": []
}
```

Each asset object must have:

```text
logical_name
kind
portable_requirement
resolved_source
runtime_owner
role
version_or_digest
verification
reacquire_method
last_verified_at
```

Minimum assets: CPython, uv, Ollama, exact Nomic digest, exact MiniLM digest if acceptance tests still use it, tokenizer measurement artifact/profile, and Git only if the accepted public-genesis workflow relies on a specific capability. Do not duplicate all packages from `uv.lock`.

For Nomic additionally record provider readback:

```text
effective_context_window=<actual verified int or null>
truncate_policy=FAIL_CLOSED_PROVIDER_REJECTION
bounded_fact_2000_bytes=PASS
bounded_query_512_bytes=PASS
```

The two bounded-input fields must not be marked PASS unless Task 4 live probes actually passed.

- [ ] **Step 9: Add repository-contract tests for governance files**

Extend `tests/unit/test_repository_contract.py` to assert these files exist and that `TASKBOARD.md` does not contain a versioned taskboard filename as its authority.

- [ ] **Step 10: Run unit tests and documentation hygiene checks**

```bash
uv run pytest tests/unit -q
git diff --check
if grep -RIn --exclude-dir=.git -E '/home/habib1001|/mnt/c/Users/' README.md AGENTS.md SECURITY.md docs; then
  exit 1
fi
```

Expected: no private absolute workstation path in public docs.

- [ ] **Step 11: Commit governance/public documentation**

```bash
git add AGENTS.md TASKBOARD.md README.md LICENSE SECURITY.md environment-provenance.json docs tests/unit/test_repository_contract.py
git diff --check
git commit -m "docs: establish FactLane repository governance"
```

---

### Task 7: Add the Minimal Recurring CI and Public Hygiene Gates

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `tests/unit/test_repository_contract.py`
- Modify if needed: `.gitignore`

**Interfaces:**
- Consumes: locked FactLane package/test environment.
- Produces: one recurring Python 3.11 CI gate; no release/deploy automation.

- [ ] **Step 1: Create `.github/workflows/ci.yml`**

Use exactly one job on Ubuntu and Python 3.11. Required steps:

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: uv sync --frozen --dev
      - run: uv run pytest -q
      - run: uv run python -c "import factlane; print(factlane.__name__)"
      - run: uv run factlane --help
```

Do not add Ollama/model download, Docker, GPU, release, Dependabot, or deployment automation in genesis.

- [ ] **Step 2: Add static public-tree hygiene tests**

Add assertions for:

```text
no src/*.egg-info
no tracked .db/.sqlite/.sqlite3
no tracked pilot-evidence
no old package directory
no private evidence archive
five tool names unchanged
```

Use `git ls-files` in a subprocess only in repository-contract tests, not in runtime code.

- [ ] **Step 3: Run the complete local CI-equivalent sequence**

```bash
uv sync --frozen --dev
uv run pytest -q
uv run python -c 'import factlane; print(factlane.__name__)'
uv run factlane --help
git diff --check
```

- [ ] **Step 4: Run source/public secret and identity scans**

Run:

```bash
if git grep -n -I -E \
  '(BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})'; then
  exit 1
fi

if git grep -n -I -E \
  '(one-linux-codex-memory|one_linux_codex_memory|One Linux Codex)' \
  -- ':!docs/PROJECT_HISTORY.md' ':!docs/superpowers/**'; then
  exit 1
fi
```

The old pilot identity is permitted only in the explicit history/spec/plan record, never product runtime or current docs.

- [ ] **Step 5: Commit the CI baseline**

```bash
git add .github tests .gitignore
git diff --check
git commit -m "ci: add FactLane genesis verification"
```

---

### Task 8: Final Acceptance, Canonical Taskboard Reconciliation, and One-Commit Public Genesis

**Files:**
- Modify: `TASKBOARD.md`
- Modify as evidence requires: `environment-provenance.json`, `docs/PROJECT_HISTORY.md`
- No product feature expansion.

**Interfaces:**
- Consumes: all prior private implementation commits and acceptance evidence.
- Produces: one verified parentless public `main` commit in `Habib1001-m/factlane`, then stop for Owner review.

- [ ] **Step 1: Freshly verify the complete amended-spec acceptance checklist**

Run at minimum:

```bash
uv sync --frozen --dev
uv run pytest -q
uv run python -c 'import factlane; print(factlane.__name__)'
uv run factlane --help
git diff --check
git status --short
```

Also verify programmatically:

```text
PRODUCT_NAME=FactLane
PACKAGE_IMPORT=factlane
CLI=factlane
MCP_TOOL_COUNT=5
TRACKED_GENERATED_EGG_INFO=0
NOMIC_TASK_PREFIX_REGRESSION_TEST=PASS
EMBEDDING_OVERFLOW_FAILS_CLOSED=PASS
BACKEND_PIN_PRESERVED=YES
EXACT_PIN_LICENSE_VERIFICATION=PASS
UPSTREAM_SQLITE_CONCURRENCY_REUSE_AUDITED=PASS
PUBLIC_SECRET_SCAN=PASS
LIVE_HOST_MUTATION=NONE
S6B_4C_STARTED=NO
```

Any failed line is a HOLD; do not create the public commit.

- [ ] **Step 2: Reconcile `TASKBOARD.md` in place before public export**

Increment only its internal version, for example:

```text
TASKBOARD_VERSION=2
```

Append a dated reconciliation entry with:

```text
FACTLANE_CANONICAL_GENESIS_IMPLEMENTATION=VERIFIED_CANDIDATE
S6B_4C=BLOCKED_PENDING_OWNER_REVIEW
```

Record the private implementation HEAD and evidence hashes. Do not create a new taskboard filename.

- [ ] **Step 3: Commit final taskboard/provenance reconciliation on the private implementation branch**

```bash
git add TASKBOARD.md environment-provenance.json docs/PROJECT_HISTORY.md
git diff --check
git commit -m "docs: reconcile verified FactLane genesis candidate"
```

Expected: implementation branch clean after commit.

- [ ] **Step 4: Re-run full verification against the exact candidate HEAD**

Run all Task 8 Step 1 commands again **after** the reconciliation commit. Record:

```bash
git rev-parse HEAD
git status --porcelain
```

Do not use verification from a pre-commit tree as final evidence.

- [ ] **Step 5: Export the verified tree without private Git history**

Create a disposable directory and export tracked tree bytes only:

```bash
CANDIDATE_HEAD=$(git rev-parse HEAD)
PUBLIC_TREE=/tmp/factlane-public-genesis
rm -rf "$PUBLIC_TREE"
mkdir -p "$PUBLIC_TREE"
git archive "$CANDIDATE_HEAD" | tar -x -C "$PUBLIC_TREE"
```

Verify no `.git` and no ignored/runtime state arrived with the archive.

- [ ] **Step 6: Initialize a new parentless public Git history**

Inside `/tmp/factlane-public-genesis`:

```bash
git init -b main
git remote add origin https://github.com/Habib1001-m/factlane.git
git add -A
git diff --cached --check
git commit -m "feat: establish FactLane canonical public genesis"
test "$(git rev-list --count HEAD)" -eq 1
test "$(git rev-list --parents -n 1 HEAD | wc -w)" -eq 1
```

The last assertion proves the genesis commit has no parent.

- [ ] **Step 7: Verify the empty remote again immediately before push**

```bash
test -z "$(git ls-remote --heads --tags origin)"
```

If not empty, HOLD. Never force-push.

- [ ] **Step 8: Push the canonical public genesis**

```bash
git push -u origin main
```

This is the only public push in the genesis slice.

- [ ] **Step 9: Verify remote tree and CI on the pushed SHA**

Read back from GitHub:

```text
REMOTE_DEFAULT_BRANCH=main
REMOTE_HEAD=<must equal local public genesis SHA>
REMOTE_COMMIT_COUNT=1
CI_ON_REMOTE_HEAD=PASS
```

Use GitHub read APIs/actions status, not a local assumption. If CI fails, do not start S6B.4C; remediate through a normal task branch/PR or report HOLD according to the failure.

- [ ] **Step 10: Stop for Owner review**

Final state must be:

```text
FACTLANE_CANONICAL_PUBLIC_GENESIS=PASS
PUBLIC_REPO=Habib1001-m/factlane
PUBLIC_HISTORY_COMMIT_COUNT=1
LIVE_CODEX_CONFIG_MUTATION=NONE
LIVE_HERMES_MUTATION=NONE
GLOBAL_MCP_REGISTRATION=NONE
REAL_MEMORY_MUTATION=NONE
LEGACY_DATA_MUTATION=NONE
KNOWLEDGE_MUTATION=NONE
S6B_4C_STARTED=NO
NEXT_SAFE_ACTION=OWNER_REVIEW_BEFORE_S6B_4C
```

No 4C work begins in this plan.

---

## Plan Self-Review

### Spec coverage

- Clean parentless public genesis: Task 8.
- FactLane naming/package/CLI/server identity: Task 2.
- Generated egg-info removal and pilot-runtime separation: Task 3.
- Nomic exactly-once prefixes + legacy double-prefix fixture correction: Tasks 3–4.
- Fail-closed embedding truncation/context evidence: Task 4.
- Backend primitive reuse and compatibility boundary: Task 5.
- AGENTS/TASKBOARD/governance/environment/history/security/license/provenance: Task 6.
- Single-board in-place version/update policy: Tasks 6 and 8.
- Minimal CI, package/import/CLI checks, public secret/hygiene scan: Task 7.
- Exact-pin license and archive custody: Task 6.
- No S6B.4C/live host/legacy/Knowledge expansion: Global Constraints + Task 8 stop boundary.

### Intentional deferrals

The following are **not** missing plan coverage; the approved spec explicitly defers them:

```text
HOST_IDENTITY_BINDING -> S6B.4C
MULTI_CLIENT_WRITE_COORDINATION / revision CAS -> S6B.4C
ASYNC_EMBEDDING_CONCURRENCY -> pre/within S6B.4C gate
PROCESS_KILL_CRASH_INJECTION -> S6B.4C
VEC0 RETENTION/COMPACTION -> S6B.4D
PRODUCTION_EMBEDDING_PROFILE -> S6B.5 / production-entry decision
CODEX_EXACT_TOKEN_EQUIVALENCE -> before production bootstrap or explicit disposition
```

### Placeholder/type consistency

This plan uses the current codebase symbols and exact planned paths. Runtime-derived provenance values are gathered by command/readback rather than represented by fake static values. No task assumes an API that is intentionally deferred to S6B.4C.
