from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import uuid

import pytest

import factlane.owner_database as owner_database_module
from factlane.contract import AdapterError
from factlane.owner_database import (
    LOGICAL_FORMAT,
    LOGICAL_IMPORT_MAX_BYTES,
    ArtifactRegistry,
    apply_logical_import,
    build_logical_export,
    create_consistent_backup,
    create_registered_logical_export,
    preview_logical_import,
    probe_logical_import_destination,
    validate_logical_document,
    verify_registered_backup,
)


def _uuid(index: int) -> str:
    return str(uuid.UUID(int=index))


def _provenance(seed: str = "a") -> dict[str, object]:
    return {
        "source_class": "TEST",
        "source_ref": f"fixture:{seed}",
        "source_hash": seed[0] * 64,
        "review_ref": f"review:{seed}",
        "extraction_method": "fixture",
    }


def _source_record(
    index: int,
    *,
    scope: dict[str, object] | None = None,
    lifecycle: str = "VALIDATED_CURRENT",
    verified_by: str = "OWNER",
    active: bool = True,
) -> dict[str, object]:
    scope = scope or {"scope": "PROJECT", "project_id": "factlane"}
    return {
        "record_id": _uuid(10_000 + index),
        "memory_id": _uuid(20_000 + index),
        "revision": 1,
        "parent_record_id": None,
        **scope,
        "memory_type": "WORKFLOW_RULE" if scope["scope"] == "CROSS_PROJECT_WORKFLOW" else "PROJECT_LEARNED_FACT",
        "fact": f"Logical export fixture {index}.",
        "tags": [f"subject:fixture-{index}"],
        "source_provenance": _provenance(chr(97 + (index % 20))),
        "freshness_policy": {"kind": "manual"},
        "source_timestamp": "2026-11-05T20:00:00Z",
        "lifecycle_state": lifecycle,
        "verified_by": verified_by,
        "supersedes": [],
        "active": active,
    }


def _logical_bytes(records: list[dict[str, object]]) -> bytes:
    return build_logical_export(
        records,
        factlane_version="0.1.7",
        storage_contract_version=3,
        embedding_profile_id="owner-test-profile",
        exported_at="2026-11-05T20:30:00Z",
    )


def test_consistent_backup_captures_committed_wal_and_registers_private_receipt(tmp_path: Path) -> None:
    source = tmp_path / "active.db"
    artifacts = tmp_path / "artifacts"
    conn = sqlite3.connect(source)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta VALUES ('contract_version', '3')")
        conn.execute(
            "INSERT INTO adapter_meta VALUES ('embedding_profile', ?)",
            (json.dumps({"profile_id": "profile-a", "store_key": "profile-a"}),),
        )
        conn.execute("CREATE TABLE facts (value TEXT NOT NULL)")
        conn.execute("INSERT INTO facts VALUES ('main')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("INSERT INTO facts VALUES ('committed-in-wal')")
        conn.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0

        registry = ArtifactRegistry(artifacts)
        receipt = create_consistent_backup(source, registry, artifact_id="backup-wal")

        assert receipt["artifact_id"] == "backup-wal"
        assert receipt["kind"] == "SQLITE_BACKUP"
        assert receipt["storage_contract_version"] == 3
        assert receipt["embedding_profile"]["profile_id"] == "profile-a"
        assert receipt["verification"]["fresh_reopen"] is True
        assert receipt["verification"]["readable"] is True
        assert receipt["verification"]["integrity_check"] == "ok"
        assert receipt["verification"]["verified_at"].endswith("Z")
        assert receipt["sha256"] == registry.get("backup-wal")["sha256"]
        backup = artifacts / receipt["relative_path"]
        assert backup.is_file()
        assert os.stat(backup).st_mode & 0o777 == 0o600
        assert os.stat(artifacts).st_mode & 0o777 == 0o700
        with sqlite3.connect(backup) as restored:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored.execute("SELECT value FROM facts ORDER BY rowid").fetchall() == [
                ("main",),
                ("committed-in-wal",),
            ]
    finally:
        conn.close()


def test_registered_artifact_listing_metadata_and_exact_preview_delete_are_path_free(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifacts")
    first = registry.managed_path("one.sqlite3")
    second = registry.managed_path("two.json")
    first.write_bytes(b"backup-one")
    second.write_bytes(b'{"format":"fixture"}')
    one = registry.register(first, kind="SQLITE_BACKUP", artifact_id="one", metadata={"compatibility": "READY"})
    registry.register(second, kind="LOGICAL_EXPORT", artifact_id="two")

    listing = registry.list_metadata()
    assert [item["artifact_id"] for item in listing] == ["one", "two"]
    assert all("relative_path" not in item for item in listing)
    assert all("path" not in item for item in listing)
    assert registry.metadata("one") == {
        key: value for key, value in one.items() if key != "relative_path"
    }

    preview = registry.preview_delete("one")
    assert preview == {
        "artifact_id": "one",
        "sha256": one["sha256"],
        "size_bytes": one["size_bytes"],
        "kind": "SQLITE_BACKUP",
    }
    sentinel = tmp_path / "sentinel.sqlite3"
    sentinel.write_bytes(b"keep")
    with pytest.raises(AdapterError, match="preview"):
        registry.delete_previewed({**preview, "size_bytes": preview["size_bytes"] + 1})
    with pytest.raises(AdapterError, match="preview"):
        registry.delete_previewed({**preview, "path": str(sentinel)})
    assert first.exists()
    assert sentinel.read_bytes() == b"keep"

    deleted = registry.delete_previewed(preview)
    assert deleted["artifact_id"] == "one"
    assert not first.exists()
    assert sentinel.read_bytes() == b"keep"
    assert [item["artifact_id"] for item in registry.list_metadata()] == ["two"]


def test_registered_artifact_delete_preview_detects_file_or_receipt_drift(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifacts")
    artifact = registry.managed_path("one.sqlite3")
    artifact.write_bytes(b"first")
    receipt = registry.register(artifact, kind="SQLITE_BACKUP", artifact_id="one")
    preview = registry.preview_delete("one")

    artifact.write_bytes(b"other")
    assert artifact.stat().st_size == receipt["size_bytes"]
    with pytest.raises(AdapterError, match="checksum"):
        registry.delete_previewed(preview)
    assert artifact.exists()


def test_registered_logical_export_is_owner_private_and_scope_validated(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifacts")
    records = [
        _source_record(1, scope={"scope": "PROJECT", "project_id": "factlane", "worktree_id": "wt-a"}),
        _source_record(2, scope={"scope": "CROSS_PROJECT_WORKFLOW"}),
    ]
    receipt = create_registered_logical_export(
        records,
        registry,
        factlane_version="0.1.7",
        storage_contract_version=3,
        embedding_profile_id="owner-test-profile",
        artifact_id="logical-one",
        exported_at="2026-11-05T20:30:00Z",
    )

    assert receipt["artifact_id"] == "logical-one"
    assert receipt["kind"] == "LOGICAL_EXPORT"
    assert receipt["logical_format"] == LOGICAL_FORMAT
    assert receipt["record_count"] == 2
    assert receipt["exact_scopes"] == [
        {"scope": "PROJECT", "project_id": "factlane", "worktree_id": "wt-a"},
        {"scope": "CROSS_PROJECT_WORKFLOW"},
    ]
    artifact = registry.root / receipt["relative_path"]
    assert artifact.is_file()
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert validate_logical_document(artifact.read_bytes())["records"][0]["scope"] == {
        "scope": "PROJECT",
        "project_id": "factlane",
        "worktree_id": "wt-a",
    }

    invalid = [_source_record(3, scope={"scope": "CROSS_PROJECT_WORKFLOW"})]
    invalid[0]["project_id"] = "must-not-be-dropped"
    with pytest.raises(AdapterError, match="CROSS_PROJECT_WORKFLOW"):
        create_registered_logical_export(
            invalid,
            registry,
            factlane_version="0.1.7",
            storage_contract_version=3,
            embedding_profile_id="owner-test-profile",
            artifact_id="logical-invalid",
        )
    assert not registry.managed_path("export-logical-invalid.json").exists()


def test_verify_registered_backup_reopens_read_only_and_reports_current_integrity(tmp_path: Path) -> None:
    source = tmp_path / "active.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta VALUES ('contract_version', '3')")
        conn.execute("CREATE TABLE facts (value TEXT NOT NULL)")
        conn.execute("INSERT INTO facts VALUES ('verified')")
        conn.commit()
    finally:
        conn.close()

    registry = ArtifactRegistry(tmp_path / "artifacts")
    receipt = create_consistent_backup(source, registry, artifact_id="verify-me")
    proof = verify_registered_backup(registry, "verify-me")
    assert proof["artifact_id"] == "verify-me"
    assert proof["sha256"] == receipt["sha256"]
    assert proof["size_bytes"] == receipt["size_bytes"]
    assert proof["kind"] == "SQLITE_BACKUP"
    assert proof["fresh_reopen"] is True
    assert proof["readable"] is True
    assert proof["integrity_check"] == "ok"
    assert proof["storage_contract_version"] == 3

    export = registry.managed_path("not-backup.json")
    export.write_bytes(b"{}")
    registry.register(export, kind="LOGICAL_EXPORT", artifact_id="not-backup")
    with pytest.raises(AdapterError, match="backup"):
        verify_registered_backup(registry, "not-backup")


def test_restore_upload_promotion_preserves_identity_reverifies_and_is_idempotent(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifacts")
    artifact = registry.managed_path("restore-upload.sqlite3")
    conn = sqlite3.connect(artifact)
    try:
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta VALUES ('contract_version', '3')")
        conn.execute(
            "INSERT INTO adapter_meta VALUES ('embedding_profile', ?)",
            (json.dumps({"profile_id": "restore-upload", "store_key": "restore-upload"}),),
        )
        conn.execute("CREATE TABLE facts(value TEXT NOT NULL)")
        conn.execute("INSERT INTO facts VALUES ('restore-me')")
        conn.commit()
    finally:
        conn.close()
    uploaded = registry.register(
        artifact,
        kind="RESTORE_UPLOAD",
        artifact_id="restore-one",
        metadata={"upload_id": _uuid(501), "declared_size_bytes": artifact.stat().st_size},
    )

    promoted = registry.promote_restore_upload("restore-one")

    assert promoted["artifact_id"] == uploaded["artifact_id"]
    assert promoted["relative_path"] == uploaded["relative_path"]
    assert promoted["sha256"] == uploaded["sha256"]
    assert promoted["size_bytes"] == uploaded["size_bytes"]
    assert promoted["kind"] == "SQLITE_BACKUP"
    assert promoted["storage_contract_version"] == 3
    assert promoted["embedding_profile"]["profile_id"] == "restore-upload"
    assert promoted["verification"]["fresh_reopen"] is True
    assert promoted["promoted_from_kind"] == "RESTORE_UPLOAD"
    assert registry.promote_restore_upload("restore-one") == promoted


def test_restore_upload_promotion_revalidates_exact_current_receipt_after_fresh_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ArtifactRegistry(tmp_path / "artifacts")
    artifact = registry.managed_path("restore-upload.sqlite3")
    conn = sqlite3.connect(artifact)
    try:
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta VALUES ('contract_version', '3')")
        conn.commit()
    finally:
        conn.close()
    registry.register(artifact, kind="RESTORE_UPLOAD", artifact_id="restore-race")
    original_verify = owner_database_module._fresh_backup_verification

    def verify_then_tamper(path: Path) -> dict[str, object]:
        result = original_verify(path)
        path.write_bytes(path.read_bytes() + b"tamper")
        return result

    monkeypatch.setattr(owner_database_module, "_fresh_backup_verification", verify_then_tamper)

    with pytest.raises(AdapterError) as caught:
        registry.promote_restore_upload("restore-race")
    assert caught.value.code == "ARTIFACT_CHECKSUM_MISMATCH"
    assert registry._load()["restore-race"]["kind"] == "RESTORE_UPLOAD"


def test_shared_registry_serializes_concurrent_restore_promotion(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifacts")
    artifact = registry.managed_path("restore-upload.sqlite3")
    conn = sqlite3.connect(artifact)
    try:
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta VALUES ('contract_version', '3')")
        conn.commit()
    finally:
        conn.close()
    uploaded = registry.register(artifact, kind="RESTORE_UPLOAD", artifact_id="restore-concurrent")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: registry.promote_restore_upload("restore-concurrent"), range(2)))

    assert results[0] == results[1]
    assert results[0]["kind"] == "SQLITE_BACKUP"
    assert results[0]["sha256"] == uploaded["sha256"]


def test_artifact_deletion_accepts_registered_identity_only_and_never_browser_paths(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    registered = root / "one.sqlite3"
    registered.write_bytes(b"registered")
    sentinel = tmp_path / "sentinel.sqlite3"
    sentinel.write_bytes(b"keep")
    registry = ArtifactRegistry(root)
    receipt = registry.register(registered, kind="SQLITE_BACKUP", artifact_id="one")

    with pytest.raises(AdapterError):
        registry.delete("../sentinel.sqlite3")
    with pytest.raises(AdapterError, match="managed artifact"):
        registry.register(sentinel, kind="SQLITE_BACKUP", artifact_id="outside")

    deleted = registry.delete("one", expected_sha256=receipt["sha256"])
    assert deleted["artifact_id"] == "one"
    assert not registered.exists()
    assert sentinel.read_bytes() == b"keep"


def test_registered_artifact_symlink_replacement_is_rejected_without_following_target(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    artifact = root / "one.sqlite3"
    artifact.write_bytes(b"registered")
    sentinel = tmp_path / "sentinel.sqlite3"
    sentinel.write_bytes(b"keep")
    registry = ArtifactRegistry(root)
    registry.register(artifact, kind="SQLITE_BACKUP", artifact_id="one")

    artifact.unlink()
    artifact.symlink_to(sentinel)
    with pytest.raises(AdapterError, match="regular managed file"):
        registry.delete("one")
    assert sentinel.read_bytes() == b"keep"


def test_failed_backup_never_publishes_or_leaves_a_staged_artifact(tmp_path: Path) -> None:
    source = tmp_path / "broken.db"
    source.write_bytes(b"not-a-sqlite-database")
    registry = ArtifactRegistry(tmp_path / "artifacts")

    with pytest.raises(AdapterError):
        create_consistent_backup(source, registry, artifact_id="broken")

    assert not (registry.root / "backup-broken.sqlite3").exists()
    with pytest.raises(AdapterError, match="not registered"):
        registry.get("broken")


def test_logical_round_trip_preserves_all_valid_five_field_scope_identities() -> None:
    records = [
        _source_record(1, scope={"scope": "GLOBAL_USER", "agent_id": "hermes"}),
        _source_record(
            2,
            scope={"scope": "PROJECT", "project_id": "factlane", "worktree_id": "worktree-a", "agent_id": "hermes"},
        ),
        _source_record(
            3,
            scope={
                "scope": "WORKFLOW",
                "project_id": "factlane",
                "worktree_id": "a",
                "workflow_id": "review",
                "agent_id": "hermes",
            },
        ),
        _source_record(
            4,
            scope={
                "scope": "TOOL_ENVIRONMENT",
                "project_id": "factlane",
                "worktree_id": "a",
                "workflow_id": "review",
                "agent_id": "hermes",
            },
        ),
        _source_record(
            5,
            scope={
                "scope": "CROSS_PROJECT_WORKFLOW",
                "project_id": None,
                "worktree_id": None,
                "workflow_id": None,
                "agent_id": None,
            },
        ),
    ]

    document = validate_logical_document(_logical_bytes(records))
    scopes = [record["scope"] for record in document["records"]]
    assert scopes == [
        {"scope": "GLOBAL_USER", "agent_id": "hermes"},
        {"scope": "PROJECT", "project_id": "factlane", "worktree_id": "worktree-a", "agent_id": "hermes"},
        {
            "scope": "WORKFLOW",
            "project_id": "factlane",
            "worktree_id": "a",
            "workflow_id": "review",
            "agent_id": "hermes",
        },
        {
            "scope": "TOOL_ENVIRONMENT",
            "project_id": "factlane",
            "worktree_id": "a",
            "workflow_id": "review",
            "agent_id": "hermes",
        },
        {"scope": "CROSS_PROJECT_WORKFLOW"},
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["records"][0].__setitem__("source_revision", True),
        lambda doc: doc["records"][0].__setitem__("active", "true"),
        lambda doc: doc["records"][0].__setitem__("supersedes_source_memory_ids", None),
        lambda doc: doc["records"][0]["scope"].__setitem__("workflow_id", "review"),
        lambda doc: doc["records"][0].__setitem__("source_lifecycle", "REVOKED") or doc["records"][0].__setitem__("active", True),
        lambda doc: doc["records"][0].__setitem__("source_timestamp", None),
        lambda doc: doc["records"][0].__setitem__("unexpected", "field"),
    ],
)
def test_logical_validation_rejects_ambiguous_or_forbidden_types_and_scope(mutate) -> None:
    raw = json.loads(_logical_bytes([_source_record(1)]))
    mutate(raw)
    with pytest.raises(AdapterError):
        validate_logical_document(json.dumps(raw).encode())


def test_cross_project_workflow_rejects_even_null_raw_identity_key() -> None:
    raw = json.loads(
        _logical_bytes([_source_record(1, scope={"scope": "CROSS_PROJECT_WORKFLOW"})])
    )
    raw["records"][0]["scope"]["agent_id"] = None
    with pytest.raises(AdapterError, match="absent"):
        validate_logical_document(json.dumps(raw).encode())


def test_logical_validation_rejects_duplicate_record_ids_and_bounded_limits() -> None:
    raw = json.loads(_logical_bytes([_source_record(1), _source_record(2)]))
    raw["records"][1]["source_record_id"] = raw["records"][0]["source_record_id"]
    with pytest.raises(AdapterError, match="duplicate"):
        validate_logical_document(json.dumps(raw).encode())

    too_many = [_source_record(index + 1) for index in range(1001)]
    with pytest.raises(AdapterError, match="1000"):
        _logical_bytes(too_many)

    valid = _logical_bytes([_source_record(1)])
    oversized = valid + b" " * (LOGICAL_IMPORT_MAX_BYTES - len(valid) + 1)
    with pytest.raises(AdapterError, match="10 MiB"):
        validate_logical_document(oversized)


def test_import_preview_keeps_source_approval_as_external_claim_and_terminal_history_inactive() -> None:
    active = _source_record(1, lifecycle="VALIDATED_CURRENT", verified_by="OWNER", active=True)
    revoked = _source_record(2, lifecycle="REVOKED", verified_by="OWNER", active=False)
    preview = preview_logical_import(_logical_bytes([active, revoked]))

    assert preview["format"] == LOGICAL_FORMAT
    assert preview["record_count"] == 2
    assert preview["candidate_admission_count"] == 1
    assert preview["external_history_count"] == 1
    admission = preview["candidate_admissions"][0]
    assert admission["target_lifecycle"] == "CANDIDATE"
    assert admission["target_verified_by"] == "UNVERIFIED"
    assert admission["target_authority"] == "UNRESOLVED"
    assert admission["scope"] == {"scope": "PROJECT", "project_id": "factlane"}
    assert admission["external_source_claim"]["source_lifecycle"] == "VALIDATED_CURRENT"
    assert admission["external_source_claim"]["source_verified_by"] == "OWNER"
    assert preview["source_approval_policy"] == "EXTERNAL_CLAIM_ONLY"


def test_destination_probe_classifies_each_candidate_in_its_exact_scope_without_mutation() -> None:
    records = [
        _source_record(1, scope={"scope": "GLOBAL_USER", "agent_id": "hermes"}),
        _source_record(2, scope={"scope": "PROJECT", "project_id": "factlane", "worktree_id": "wt-a"}),
        _source_record(
            3,
            scope={
                "scope": "WORKFLOW",
                "project_id": "factlane",
                "worktree_id": "wt-a",
                "workflow_id": "review",
                "agent_id": "hermes",
            },
        ),
        _source_record(
            4,
            scope={
                "scope": "TOOL_ENVIRONMENT",
                "project_id": "factlane",
                "workflow_id": "review",
                "agent_id": "hermes",
            },
        ),
        _source_record(5, scope={"scope": "CROSS_PROJECT_WORKFLOW"}),
    ]
    payload = _logical_bytes(records)
    seen: list[dict[str, object]] = []

    async def probe(request):
        seen.append(request)
        source_record_id = request["source_record_id"]
        if source_record_id == _uuid(10_001):
            return {
                "exact": {
                    "record_id": "dest-exact",
                    "scope": request["scope"],
                    "native_content_hash": request["native_content_hash"],
                    "contradiction_key": request["contradiction_key"],
                    "fact": request["fact"],
                },
                "contradictions": [],
            }
        if source_record_id == _uuid(10_002):
            return {
                "exact": None,
                "contradictions": [
                    {
                        "record_id": "dest-equivalent",
                        "scope": request["scope"],
                        "native_content_hash": "f" * 64,
                        "contradiction_key": request["contradiction_key"],
                        "fact": "  LOGICAL   EXPORT fixture 2. ",
                    }
                ],
            }
        if source_record_id == _uuid(10_003):
            return {
                "exact": None,
                "contradictions": [
                    {
                        "record_id": "dest-conflict",
                        "scope": request["scope"],
                        "native_content_hash": "e" * 64,
                        "contradiction_key": request["contradiction_key"],
                        "fact": "A different fact for the same contradiction identity.",
                    }
                ],
            }
        return {"exact": None, "contradictions": []}

    async def run() -> None:
        preview = await probe_logical_import_destination(payload, destination_probe=probe)
        assert [item["classification"] for item in preview["destination_results"]] == [
            "EXACT_DUPLICATE",
            "EQUIVALENT",
            "CONFLICT",
            "NEW",
            "NEW",
        ]
        assert preview["destination_summary"] == {
            "EXACT_DUPLICATE": 1,
            "EQUIVALENT": 1,
            "CONFLICT": 1,
            "NEW": 2,
        }
        assert preview["destination_analysis"] == "COMPLETE"
        assert preview["destination_duplicates"] == [_uuid(10_001), _uuid(10_002)]
        assert preview["destination_conflicts"] == [_uuid(10_003)]

        assert [item["scope"] for item in seen] == [
            {"scope": "GLOBAL_USER", "agent_id": "hermes"},
            {"scope": "PROJECT", "project_id": "factlane", "worktree_id": "wt-a"},
            {
                "scope": "WORKFLOW",
                "project_id": "factlane",
                "worktree_id": "wt-a",
                "workflow_id": "review",
                "agent_id": "hermes",
            },
            {
                "scope": "TOOL_ENVIRONMENT",
                "project_id": "factlane",
                "workflow_id": "review",
                "agent_id": "hermes",
            },
            {"scope": "CROSS_PROJECT_WORKFLOW"},
        ]
        assert all(item["mutation_allowed"] is False for item in seen)

    asyncio.run(run())


def test_destination_probe_rejects_cross_scope_or_malformed_probe_results() -> None:
    payload = _logical_bytes([_source_record(1, scope={"scope": "PROJECT", "project_id": "factlane"})])

    async def cross_scope(request):
        return {
            "exact": None,
            "contradictions": [
                {
                    "record_id": "wrong-scope",
                    "scope": {"scope": "PROJECT", "project_id": "other-project"},
                    "native_content_hash": "a" * 64,
                    "contradiction_key": request["contradiction_key"],
                    "fact": request["fact"],
                }
            ],
        }

    async def malformed(_request):
        return {"exact": None, "contradictions": "not-a-list"}

    async def run() -> None:
        with pytest.raises(AdapterError, match="exact scope"):
            await probe_logical_import_destination(payload, destination_probe=cross_scope)
        with pytest.raises(AdapterError, match="probe"):
            await probe_logical_import_destination(payload, destination_probe=malformed)

    asyncio.run(run())


def test_import_apply_prepares_vectors_before_single_atomic_candidate_callback_and_binds_replay_identity() -> None:
    payload = _logical_bytes(
        [
            _source_record(1, scope={"scope": "GLOBAL_USER", "agent_id": "hermes"}),
            _source_record(2, scope={"scope": "PROJECT", "project_id": "factlane"}),
        ]
    )
    calls: list[tuple[str, object]] = []
    outcomes: dict[str, dict[str, object]] = {}

    async def prepare(admissions, binding):
        calls.append(("prepare", binding.copy()))
        return {item["source_record_id"]: [1.0, 0.0] for item in admissions}

    async def admit(admissions, prepared, binding):
        calls.append(("admit", binding.copy()))
        existing = outcomes.get(binding["operation_id"])
        if existing is not None:
            if existing["idempotency_key"] != binding["idempotency_key"]:
                raise AdapterError("IDEMPOTENCY_CONFLICT", "operation identity was already used for different input")
            return {**existing, "idempotent_replay": True}
        assert len(admissions) == 2
        assert admissions[0]["scope"] == {"scope": "GLOBAL_USER", "agent_id": "hermes"}
        assert admissions[0]["source_provenance"]["source_class"] == "LOGICAL_IMPORT"
        assert admissions[0]["source_provenance"]["source_hash"] == binding["file_digest"]
        assert admissions[0]["external_source_claim"]["source_verified_by"] == "OWNER"
        assert set(prepared) == {item["source_record_id"] for item in admissions}
        result = {
            "status": "OK",
            "idempotency_key": binding["idempotency_key"],
            "admitted": len(admissions),
            "idempotent_replay": False,
        }
        outcomes[binding["operation_id"]] = result
        return result

    async def run() -> None:
        first = await apply_logical_import(
            payload,
            operation_id="import-op-1",
            vector_prepare=prepare,
            candidate_admit=admit,
        )
        second = await apply_logical_import(
            payload,
            operation_id="import-op-1",
            vector_prepare=prepare,
            candidate_admit=admit,
        )
        assert first["admitted"] == 2
        assert second["idempotent_replay"] is True
        prepare_binding = calls[0][1]
        admit_binding = calls[1][1]
        assert prepare_binding["file_digest"] == admit_binding["file_digest"]
        assert prepare_binding["scope_identity_digest"] == admit_binding["scope_identity_digest"]
        assert prepare_binding["idempotency_key"] == admit_binding["idempotency_key"]
        assert calls[0][0] == "prepare"
        assert calls[1][0] == "admit"

        changed = json.loads(payload)
        changed["records"][0]["fact"] = "Changed bytes for the same operation identity."
        changed_payload = json.dumps(changed, separators=(",", ":"), sort_keys=True).encode()
        with pytest.raises(AdapterError, match="different input"):
            await apply_logical_import(
                changed_payload,
                operation_id="import-op-1",
                vector_prepare=prepare,
                candidate_admit=admit,
            )

    asyncio.run(run())


def test_import_apply_never_calls_candidate_activation_when_vector_preparation_fails() -> None:
    payload = _logical_bytes([_source_record(1)])
    admitted = False

    async def prepare(_admissions, _binding):
        raise RuntimeError("provider unavailable")

    async def admit(_admissions, _prepared, _binding):
        nonlocal admitted
        admitted = True
        return {"status": "OK"}

    async def run() -> None:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await apply_logical_import(
                payload,
                operation_id="import-op-provider-failure",
                vector_prepare=prepare,
                candidate_admit=admit,
            )
        assert admitted is False

    asyncio.run(run())


def test_logical_export_rejects_cross_project_storage_row_with_meaningful_identity() -> None:
    record = _source_record(1, scope={"scope": "CROSS_PROJECT_WORKFLOW"})
    record["project_id"] = "must-not-be-dropped"
    with pytest.raises(AdapterError, match="CROSS_PROJECT_WORKFLOW"):
        _logical_bytes([record])
