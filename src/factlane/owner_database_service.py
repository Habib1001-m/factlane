from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from typing import Any, Awaitable, Callable, Mapping
import uuid

from .contract import AdapterError, LIFECYCLE_STATES, canonical_json, iso_now, parse_iso, validate_scope
from .owner_database import (
    ArtifactRegistry,
    LOGICAL_IMPORT_MAX_BYTES,
    _flush_private_file,
    _open_new_private_file,
    _open_windows_parent_guard,
    _protect_created_private_file,
    _publish_new_private_file,
    _replace_private_file,
    _unlink_private_file,
    _validate_private_regular_file,
    _write_private_bytes,
    _windows_path_guard,
    create_consistent_backup,
    create_registered_logical_export,
)
from .windows_security import WindowsSecurityError, validate_windows_path_no_reparse
from .storage import STORAGE_CONTRACT_VERSION, inspect_storage_contract_version


FULL_RESTORE_UPLOAD_MAX_BYTES = 1024 * 1024 * 1024
DOWNLOAD_READ_MAX_BYTES = 1024 * 1024
_JOB_LEDGER_MAX_BYTES = 16 * 1024 * 1024
_JOB_RESULT_MAX_BYTES = 64 * 1024
_JOB_LEDGER_NAME = ".factlane-database-jobs.json"
_STAGING_DIR_NAME = ".staging"

_UPLOAD_KINDS = {
    "LOGICAL_IMPORT": (LOGICAL_IMPORT_MAX_BYTES, "logical.json.part"),
    "RESTORE_UPLOAD": (FULL_RESTORE_UPLOAD_MAX_BYTES, "restore.sqlite3.part"),
}
_DOWNLOAD_EXTENSIONS = {
    "LOGICAL_IMPORT": ".json",
    "LOGICAL_EXPORT": ".json",
    "RESTORE_UPLOAD": ".sqlite3",
    "SQLITE_BACKUP": ".sqlite3",
}
_JOB_KINDS = frozenset(
    {
        "BACKUP_CREATE",
        "LOGICAL_EXPORT",
        "LOGICAL_IMPORT_PREVIEW",
        "LOGICAL_IMPORT_COMMIT",
        "RESTORE_PREVIEW",
        "RESTORE_COMMIT",
    }
)
_JOB_PHASES = frozenset({"QUEUED", "RUNNING", "SUCCEEDED", "FAILED"})
_TERMINAL_JOB_PHASES = frozenset({"SUCCEEDED", "FAILED"})
_UNSAFE_JOB_RESULT_KEYS = frozenset(
    {
        "content",
        "fact",
        "facts",
        "records",
        "payload",
        "grant",
        "grant_secret",
        "csrf",
        "cookie",
        "cookies",
        "token",
        "api_token",
        "secret",
        "password",
        "path",
        "relative_path",
    }
)
_HEX_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_JOB_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_RESTORE_PUBLIC_RESULT_KEYS = frozenset(
    {
        "status",
        "action",
        "operation",
        "operation_id",
        "artifact_id",
        "artifact_sha256",
        "pre_restore_artifact_id",
        "generation",
        "activation_device",
        "activation_inode",
        "ready_instance_ids",
        "reconnect_failures",
        "maintenance_participant_failures",
        "idempotent_replay",
        "post_activation_verification",
    }
)
_RESTORE_PRODUCT_PROOF_KEYS = frozenset(
    {
        "status",
        "operation_id",
        "generation",
        "database_path",
        "database_device",
        "database_inode",
        "storage_contract_version",
        "embedding_profile",
        "bounded_read_verified",
        "ready_instance_ids",
        "reconnect_failures",
        "resumed_runtime_evidence",
    }
)
_RESTORE_RUNTIME_EVIDENCE_KEYS = frozenset(
    {
        "protocol_version",
        "instance_id",
        "owner_uid",
        "pid",
        "process_start_ticks",
        "boot_id",
        "database_path",
        "device",
        "inode",
        "generation",
        "control_socket_path",
        "lifecycle_state",
        "lease_open",
    }
)


LogicalExportSelector = Callable[[dict[str, Any]], Any | Awaitable[Any]]
LogicalImportHook = Callable[[bytes, dict[str, Any]], Any | Awaitable[Any]]
BackupCreateHook = Callable[[Path, ArtifactRegistry, str], Any | Awaitable[Any]]
RestorePreviewHook = Callable[[dict[str, Any]], Any | Awaitable[Any]]
RestoreAuthorizeCallback = Callable[[str], Any | Awaitable[Any]]
RestoreApplyHook = Callable[
    [dict[str, Any], dict[str, Any], RestoreAuthorizeCallback],
    Any | Awaitable[Any],
]
FreeSpaceProvider = Callable[[Path], int]
UUIDFactory = Callable[[], uuid.UUID | str]


def _raise(code: str, message: str) -> None:
    raise AdapterError(code, message)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _default_backup_create(active_db_path: Path, registry: ArtifactRegistry, artifact_id: str) -> dict[str, Any]:
    return create_consistent_backup(active_db_path, registry, artifact_id=artifact_id)


def _canonical_uuid(value: str, field: str) -> str:
    if not isinstance(value, str):
        _raise("INVALID_ENVELOPE", f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise AdapterError("INVALID_ENVELOPE", f"{field} must be a canonical UUID") from exc
    canonical = str(parsed)
    if canonical != value:
        _raise("INVALID_ENVELOPE", f"{field} must use canonical UUID spelling")
    return canonical


def _private_directory(path: Path, *, label: str) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            _raise("DATABASE_SERVICE_PATH_INVALID", f"{label} must be a real directory, not a symlink")
        if os.name == "nt":
            _windows_path_guard(
                path,
                directory=True,
                protect=False,
                code="DATABASE_SERVICE_PATH_INVALID",
                message=f"{label} must be owner-controlled",
            )
        else:
            if path.stat().st_uid != os.getuid():
                _raise("DATABASE_SERVICE_PATH_INVALID", f"{label} must be owned by the current user")
            os.chmod(path, 0o700)
        return
    path.mkdir(parents=False, mode=0o700)
    if os.name == "nt":
        _windows_path_guard(
            path,
            directory=True,
            protect=True,
            code="DATABASE_SERVICE_PATH_INVALID",
            message=f"{label} could not be protected for the current user",
        )
    else:
        os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows publication flushes the retained source handle before and after
        # the handle-based rename. There is no portable directory-fsync equivalent.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class UploadSink:
    """One bounded owner-private upload staged under a server-generated identity."""

    def __init__(
        self,
        service: OwnerDatabaseService,
        *,
        upload_id: str,
        kind: str,
        declared_size: int,
        expected_sha256: str | None,
    ) -> None:
        self._service = service
        self.upload_id = upload_id
        self.kind = kind
        self.declared_size = declared_size
        self.expected_sha256 = expected_sha256
        suffix = _UPLOAD_KINDS[kind][1]
        self.filename = f"upload-{upload_id}-{suffix}"
        self._stage_path = service._staging_root / self.filename
        self._bytes_written = 0
        self._digest = hashlib.sha256()
        self._state = "OPEN"
        flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self._fd, self._parent_guard = _open_new_private_file(
                self._stage_path,
                flags,
                code="UPLOAD_STAGE_INVALID",
                message="upload staging file is not owner-controlled",
            )
        except FileExistsError as exc:
            raise AdapterError("UPLOAD_STAGE_COLLISION", "server-generated upload staging identity collided") from exc
        opened = os.fstat(self._fd)
        path_info = _validate_private_regular_file(
            self._stage_path,
            code="UPLOAD_STAGE_INVALID",
            message="upload staging file is not owner-controlled",
        )
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (path_info.st_dev, path_info.st_ino):
            self.abort()
            _raise("UPLOAD_STAGE_INVALID", "upload staging file is not owner-controlled")
        self._stage_identity = (opened.st_dev, opened.st_ino)

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def _require_open(self) -> int:
        if self._state != "OPEN" or self._fd is None:
            _raise("UPLOAD_CLOSED", "upload sink is no longer writable")
        return self._fd

    def write(self, data: bytes | bytearray | memoryview) -> int:
        fd = self._require_open()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            _raise("UPLOAD_INVALID", "upload chunks must be bytes")
        raw = bytes(data)
        if self._bytes_written + len(raw) > self.declared_size:
            self.abort()
            _raise("UPLOAD_SIZE_MISMATCH", "upload exceeds its declared byte size")
        total = 0
        view = memoryview(raw)
        try:
            while total < len(raw):
                written = os.write(fd, view[total:])
                if written <= 0:
                    raise OSError("upload staging write made no progress")
                piece = raw[total : total + written]
                self._digest.update(piece)
                self._bytes_written += written
                total += written
        except BaseException:
            self.abort()
            raise
        return total

    def seal(self) -> dict[str, Any]:
        fd = self._require_open()
        if self._bytes_written != self.declared_size:
            self.abort()
            _raise("UPLOAD_INCOMPLETE", "upload is incomplete for its declared byte size")
        digest = self._digest.hexdigest()
        try:
            _flush_private_file(fd)
        except BaseException:
            self.abort()
            raise
        if self.expected_sha256 is not None and digest != self.expected_sha256:
            self.abort()
            _raise("UPLOAD_CHECKSUM_MISMATCH", "upload checksum does not match the declared checksum")

        try:
            current_stage = _validate_private_regular_file(
                self._stage_path,
                code="UPLOAD_STAGE_INVALID",
                message="upload staging path changed before sealing",
            )
        except FileNotFoundError:
            self.abort()
            _raise("UPLOAD_STAGE_INVALID", "upload staging path disappeared before sealing")
        if (
            (current_stage.st_dev, current_stage.st_ino) != self._stage_identity
        ):
            self.abort()
            _raise("UPLOAD_STAGE_INVALID", "upload staging path changed before sealing")

        artifact_id = self._service._new_uuid("artifact_id")
        destination = self._service._registry.managed_path(
            self._service._artifact_storage_filename(self.kind, artifact_id)
        )
        published = False
        registered = False
        destination_parent_guard = None
        try:
            if os.name == "nt":
                destination_parent_guard = _open_windows_parent_guard(
                    destination,
                    code="UPLOAD_STAGE_INVALID",
                    message="upload publication parent is not owner-controlled",
                )
            _publish_new_private_file(
                self._stage_path,
                destination,
                source_fd=fd if os.name == "nt" else None,
                source_parent_guard=self._parent_guard,
                destination_parent_guard=destination_parent_guard,
            )
            published = True
            linked_info = _validate_private_regular_file(
                destination,
                code="UPLOAD_STAGE_INVALID",
                message="upload staging identity changed during sealing",
            )
            if (
                (linked_info.st_dev, linked_info.st_ino) != self._stage_identity
            ):
                _raise("UPLOAD_STAGE_INVALID", "upload staging identity changed during sealing")
            if os.name != "nt":
                os.chmod(destination, 0o600)
            else:
                os.close(fd)
                self._fd = None
            _fsync_directory(self._service._registry.root)
            self._service._registry.register(
                destination,
                kind=self.kind,
                artifact_id=artifact_id,
                metadata={
                    "upload_id": self.upload_id,
                    "declared_size_bytes": self.declared_size,
                },
            )
            registered = True
            metadata = self._service.artifact_metadata(artifact_id)
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
            self._fd = None
            self._state = "SEALED"
            _unlink_private_file(
                self._stage_path,
                parent_guard=self._parent_guard,
                expected_identity=self._stage_identity,
            )
            _fsync_directory(self._service._staging_root)
            if self._parent_guard is not None:
                self._parent_guard.close()
                self._parent_guard = None
            return metadata
        except BaseException:
            if published and not registered:
                _unlink_private_file(destination, parent_guard=destination_parent_guard)
            self.abort()
            raise
        finally:
            if destination_parent_guard is not None:
                destination_parent_guard.close()

    def abort(self) -> None:
        if self._state in {"ABORTED", "SEALED"}:
            return
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _unlink_private_file(
            self._stage_path,
            parent_guard=self._parent_guard,
            expected_identity=getattr(self, "_stage_identity", None),
        )
        if self._parent_guard is not None:
            self._parent_guard.close()
            self._parent_guard = None
        self._state = "ABORTED"

    def close(self) -> None:
        self.abort()

    def __enter__(self) -> UploadSink:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self._state == "OPEN":
            self.abort()


class VerifiedDownloadHandle:
    """Verified descriptor-backed download without exposing a filesystem locator."""

    def __init__(self, *, fd: int, metadata: dict[str, Any], filename: str) -> None:
        self.kind = str(metadata["kind"])
        self.size_bytes = int(metadata["size_bytes"])
        self.sha256 = str(metadata["sha256"])
        self.filename = filename
        self._fd: int | None = fd

    def read(self, maximum_bytes: int = DOWNLOAD_READ_MAX_BYTES) -> bytes:
        if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int) or not 1 <= maximum_bytes <= DOWNLOAD_READ_MAX_BYTES:
            _raise("DOWNLOAD_READ_INVALID", "download reads must use a positive bounded byte count")
        if self._fd is None:
            _raise("DOWNLOAD_CLOSED", "download handle is closed")
        return os.read(self._fd, maximum_bytes)

    def close(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        os.close(fd)

    def __enter__(self) -> VerifiedDownloadHandle:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


class OwnerDatabaseService:
    """Owner database-artifact/job facade with no caller-selected filesystem paths."""

    def __init__(
        self,
        artifacts_root: str | os.PathLike[str],
        *,
        artifact_registry: ArtifactRegistry | None = None,
        active_db_path: str | os.PathLike[str] | None = None,
        backup_create: BackupCreateHook | None = None,
        logical_export_selector: LogicalExportSelector | None = None,
        logical_export_producer: Mapping[str, Any] | None = None,
        logical_import_preview: LogicalImportHook | None = None,
        logical_import_apply: LogicalImportHook | None = None,
        restore_preview: RestorePreviewHook | None = None,
        restore_apply: RestoreApplyHook | None = None,
        free_space_provider: FreeSpaceProvider | None = None,
        uuid_factory: UUIDFactory = uuid.uuid4,
    ) -> None:
        supplied_root = Path(artifacts_root).absolute()
        if supplied_root.is_symlink():
            _raise("DATABASE_SERVICE_CONFIG_INVALID", "trusted artifacts_root cannot be a symlink")
        if os.name == "nt":
            try:
                validate_windows_path_no_reparse(supplied_root)
            except WindowsSecurityError as exc:
                raise AdapterError(
                    "DATABASE_SERVICE_CONFIG_INVALID",
                    "trusted artifacts_root contains an unsafe Windows path",
                ) from exc
            _private_directory(supplied_root, label="trusted artifacts root")
        trusted_root = supplied_root.resolve(strict=False)
        if artifact_registry is not None:
            if not isinstance(artifact_registry, ArtifactRegistry) or artifact_registry.root != trusted_root:
                _raise("DATABASE_SERVICE_CONFIG_INVALID", "injected artifact registry does not match trusted artifacts_root")
            self._registry = artifact_registry
        else:
            self._registry = ArtifactRegistry(artifacts_root)
        self._staging_root = self._registry.root / _STAGING_DIR_NAME
        _private_directory(self._staging_root, label="upload staging directory")
        self._job_ledger_path = self._registry.root / _JOB_LEDGER_NAME
        self._active_db_path: Path | None = None
        if active_db_path is not None:
            candidate = Path(active_db_path)
            if not candidate.is_absolute():
                _raise("DATABASE_SERVICE_CONFIG_INVALID", "trusted active_db_path must be absolute")
            self._active_db_path = candidate.resolve(strict=False)
        self._backup_create = backup_create or _default_backup_create
        self._logical_export_selector = logical_export_selector
        self._logical_export_producer = self._normalize_logical_export_producer(logical_export_producer)
        self._logical_import_preview = logical_import_preview
        self._logical_import_apply = logical_import_apply
        self._restore_preview = restore_preview
        self._restore_apply = restore_apply
        self._free_space_provider = free_space_provider or (lambda path: int(shutil.disk_usage(path).free))
        self._uuid_factory = uuid_factory
        self._job_lock = threading.RLock()
        self._reconcile_interrupted_jobs()

    @staticmethod
    def _normalize_logical_export_producer(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            _raise("DATABASE_SERVICE_CONFIG_INVALID", "logical export producer metadata must be an object")
        producer = dict(value)
        if set(producer) != {"factlane_version", "storage_contract_version", "embedding_profile_id"}:
            _raise("DATABASE_SERVICE_CONFIG_INVALID", "logical export producer metadata fields are not exact")
        factlane_version = producer.get("factlane_version")
        storage_contract_version = producer.get("storage_contract_version")
        embedding_profile_id = producer.get("embedding_profile_id")
        if (
            not isinstance(factlane_version, str)
            or not factlane_version
            or len(factlane_version.encode("utf-8")) > 128
            or isinstance(storage_contract_version, bool)
            or not isinstance(storage_contract_version, int)
            or storage_contract_version < 0
            or not isinstance(embedding_profile_id, str)
            or not embedding_profile_id
            or len(embedding_profile_id.encode("utf-8")) > 256
        ):
            _raise("DATABASE_SERVICE_CONFIG_INVALID", "logical export producer metadata is invalid")
        return producer

    def _new_uuid(self, field: str) -> str:
        value = str(self._uuid_factory())
        return _canonical_uuid(value, field)

    @staticmethod
    def _artifact_storage_filename(kind: str, artifact_id: str) -> str:
        if kind == "LOGICAL_IMPORT":
            return f"logical-import-{artifact_id}.json"
        if kind == "RESTORE_UPLOAD":
            return f"restore-upload-{artifact_id}.sqlite3"
        _raise("INVALID_ARTIFACT", "unsupported upload artifact kind")

    @staticmethod
    def _download_filename(metadata: Mapping[str, Any]) -> str:
        artifact_id = metadata.get("artifact_id")
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            _raise("INVALID_ARTIFACT", "artifact identity is invalid")
        kind = metadata.get("kind")
        if not isinstance(kind, str) or not kind:
            _raise("INVALID_ARTIFACT", "artifact kind is invalid")
        extension = _DOWNLOAD_EXTENSIONS.get(kind, ".bin")
        safe_kind = re.sub(r"[^a-z0-9-]", "-", kind.lower().replace("_", "-"))
        return f"factlane-{safe_kind}-{artifact_id}{extension}"

    @classmethod
    def _public_artifact_metadata(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._public_artifact_metadata(item) for item in value]
        if isinstance(value, dict):
            projected: dict[str, Any] = {}
            for key, item in value.items():
                normalized = key.casefold()
                if normalized == "path" or normalized == "relative_path" or normalized.endswith("_path"):
                    continue
                projected[key] = cls._public_artifact_metadata(item)
            return projected
        return value

    def _free_space(self) -> int:
        try:
            value = self._free_space_provider(self._staging_root)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("UPLOAD_SPACE_UNAVAILABLE", "upload free-space preflight is unavailable") from exc
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _raise("UPLOAD_SPACE_UNAVAILABLE", "upload free-space preflight returned an invalid value")
        return value

    def _begin_upload(self, *, kind: str, declared_size: int, expected_sha256: str | None) -> UploadSink:
        _private_directory(self._staging_root, label="upload staging directory")
        maximum, _stage_suffix = _UPLOAD_KINDS[kind]
        if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size <= 0:
            _raise("UPLOAD_SIZE_INVALID", "upload declared size must be a positive integer")
        if declared_size > maximum:
            limit = "10 MiB" if kind == "LOGICAL_IMPORT" else "1 GiB"
            _raise("UPLOAD_TOO_LARGE", f"upload exceeds the {limit} limit")
        digest: str | None = None
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or not _HEX_DIGEST_RE.fullmatch(expected_sha256):
                _raise("UPLOAD_CHECKSUM_INVALID", "expected upload checksum must be a SHA-256 digest")
            digest = expected_sha256.lower()
        if self._free_space() < declared_size:
            _raise("UPLOAD_INSUFFICIENT_SPACE", "insufficient free space for the declared upload")
        upload_id = self._new_uuid("upload_id")
        return UploadSink(
            self,
            upload_id=upload_id,
            kind=kind,
            declared_size=declared_size,
            expected_sha256=digest,
        )

    def begin_logical_import_upload(
        self,
        *,
        declared_size: int,
        expected_sha256: str | None = None,
    ) -> UploadSink:
        return self._begin_upload(
            kind="LOGICAL_IMPORT",
            declared_size=declared_size,
            expected_sha256=expected_sha256,
        )

    def begin_restore_upload(
        self,
        *,
        declared_size: int,
        expected_sha256: str | None = None,
    ) -> UploadSink:
        return self._begin_upload(
            kind="RESTORE_UPLOAD",
            declared_size=declared_size,
            expected_sha256=expected_sha256,
        )

    def list_artifacts(self) -> list[dict[str, Any]]:
        value = self._public_artifact_metadata(self._registry.list_metadata())
        assert isinstance(value, list)
        return value

    def list_registered_databases(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.list_artifacts()
            if item.get("kind") == "SQLITE_BACKUP" and item.get("local_database_registration") is True
        ]

    def database_status(self) -> dict[str, Any]:
        if self._active_db_path is None:
            _raise("DATABASE_STATUS_UNAVAILABLE", "active database status is not configured")
        active = self._active_db_path
        try:
            info = _validate_private_regular_file(
                active,
                code="DATABASE_STATUS_UNAVAILABLE",
                message="active database is not an owner-controlled regular file",
            )
        except FileNotFoundError as exc:
            raise AdapterError("DATABASE_STATUS_UNAVAILABLE", "active database is unavailable") from exc
        identity = hashlib.sha256(f"{info.st_dev}:{info.st_ino}".encode("ascii")).hexdigest()[:24]
        artifacts = self.list_artifacts()
        backups = [item for item in artifacts if item.get("kind") == "SQLITE_BACKUP"]
        backup_bytes = sum(
            int(item["size_bytes"])
            for item in backups
            if isinstance(item.get("size_bytes"), int) and not isinstance(item.get("size_bytes"), bool)
        )
        try:
            free_bytes: int | None = int(self._free_space_provider(self._registry.root))
        except OSError:
            free_bytes = None
        active_bytes = int(info.st_size)
        storage = {
            "database_file_bytes": active_bytes,
            "wal_file_bytes": self._file_size(Path(str(active) + "-wal")),
            "shm_file_bytes": self._file_size(Path(str(active) + "-shm")),
            "backup_count": len(backups),
            "backup_total_bytes": backup_bytes,
            "artifact_total_bytes": sum(
                int(item["size_bytes"])
                for item in artifacts
                if isinstance(item.get("size_bytes"), int) and not isinstance(item.get("size_bytes"), bool)
            ),
            "filesystem_free_bytes": free_bytes,
        }
        storage["warning"] = (
            "CAPACITY_UNKNOWN"
            if free_bytes is None
            else "LOW_SPACE_FOR_FULL_BACKUP"
            if free_bytes < active_bytes
            else None
        )
        try:
            contract = inspect_storage_contract_version(str(active))
            with tempfile.TemporaryDirectory(prefix="factlane-db-health-") as td:
                copied = Path(td) / "factlane.db"
                shutil.copy2(active, copied)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(active) + suffix)
                    if sidecar.exists():
                        shutil.copy2(sidecar, Path(str(copied) + suffix))
                connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True, timeout=5.0)
                try:
                    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                finally:
                    connection.close()
        except (AdapterError, OSError, sqlite3.Error) as exc:
            return {
                "status": "DEGRADED",
                "database_identity": f"db-{identity}",
                "compatibility": "UNKNOWN",
                "integrity_check": "unavailable",
                "storage": storage,
                "failure": {
                    "code": getattr(exc, "code", "DATABASE_STATUS_UNAVAILABLE"),
                    "action": "Run FactLane database diagnostics before maintenance.",
                },
            }
        producer = self._logical_export_producer or {}
        compatibility = "COMPATIBLE" if contract == STORAGE_CONTRACT_VERSION and integrity == "ok" else "INCOMPATIBLE"
        return {
            "status": "OK" if compatibility == "COMPATIBLE" else "DEGRADED",
            "database_identity": f"db-{identity}",
            "storage_contract_version": contract,
            "expected_storage_contract_version": STORAGE_CONTRACT_VERSION,
            "embedding_profile_id": producer.get("embedding_profile_id"),
            "factlane_version": producer.get("factlane_version"),
            "compatibility": compatibility,
            "integrity_check": integrity,
            "storage": storage,
        }

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return int(path.stat().st_size)
        except FileNotFoundError:
            return 0
        except OSError:
            return 0

    def list_jobs(
        self,
        *,
        limit: int = 100,
        kind: str | None = None,
        phase: str | None = None,
        cursor: str | None = None,
        occurred_after: str | None = None,
        occurred_before: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            _raise("JOB_FILTER_INVALID", "job history limit must be from 1 through 100")
        if kind is not None and kind not in _JOB_KINDS:
            _raise("JOB_FILTER_INVALID", "job history kind is unsupported")
        if phase is not None and phase not in _JOB_PHASES:
            _raise("JOB_FILTER_INVALID", "job history phase is unsupported")
        after_time = parse_iso(occurred_after)
        before_time = parse_iso(occurred_before)
        if after_time is not None and before_time is not None and after_time > before_time:
            _raise("JOB_FILTER_INVALID", "job history start time must not be after end time")
        jobs = [
            self._public_job_record(record)
            for record in self._load_jobs().values()
            if (kind is None or record.get("kind") == kind)
            and (phase is None or record.get("phase") == phase)
            and (after_time is None or parse_iso(str(record.get("updated_at")), required=True) >= after_time)
            and (before_time is None or parse_iso(str(record.get("updated_at")), required=True) <= before_time)
        ]
        jobs.sort(key=lambda item: (str(item["updated_at"]), str(item["job_id"])), reverse=True)
        start = 0
        if cursor is not None:
            _canonical_uuid(cursor, "cursor")
            indexes = [index for index, item in enumerate(jobs) if item["job_id"] == cursor]
            if len(indexes) != 1:
                _raise("JOB_CURSOR_INVALID", "job history cursor is stale or invalid")
            start = indexes[0] + 1
        page = jobs[start : start + limit]
        next_cursor = page[-1]["job_id"] if start + limit < len(jobs) and page else None
        return {
            "status": "OK",
            "filters": {
                "kind": kind,
                "phase": phase,
                "occurred_after": occurred_after,
                "occurred_before": occurred_before,
            },
            "items": page,
            "next_cursor": next_cursor,
        }

    def artifact_metadata(self, artifact_id: str) -> dict[str, Any]:
        value = self._public_artifact_metadata(self._registry.metadata(artifact_id))
        assert isinstance(value, dict)
        return value

    def open_download(self, artifact_id: str) -> VerifiedDownloadHandle:
        entry = self._registry.get(artifact_id)
        relative = entry.get("relative_path")
        if not isinstance(relative, str):
            _raise("ARTIFACT_REGISTRY_INVALID", "registered artifact path is invalid")
        path = self._registry.managed_path(relative)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise AdapterError("ARTIFACT_NOT_FOUND", "registered artifact could not be opened safely") from exc
        try:
            info = os.fstat(fd)
            path_info = _validate_private_regular_file(
                path,
                code="ARTIFACT_PATH_INVALID",
                message="registered artifact is not an owner-controlled regular file",
            )
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
                _raise("ARTIFACT_PATH_INVALID", "registered artifact is not an owner-controlled regular file")
            if info.st_size != entry.get("size_bytes"):
                _raise("ARTIFACT_CHECKSUM_MISMATCH", "registered artifact size no longer matches its receipt")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, DOWNLOAD_READ_MAX_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
            if digest.hexdigest() != entry.get("sha256"):
                _raise("ARTIFACT_CHECKSUM_MISMATCH", "registered artifact checksum no longer matches its receipt")
            os.lseek(fd, 0, os.SEEK_SET)
            metadata = self._public_artifact_metadata(entry)
            assert isinstance(metadata, dict)
            return VerifiedDownloadHandle(
                fd=fd,
                metadata=metadata,
                filename=self._download_filename(metadata),
            )
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _delete_preview_digest(preview: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json(preview).encode("utf-8")).hexdigest()

    def preview_artifact_delete(self, artifact_id: str) -> dict[str, Any]:
        preview = self._registry.preview_delete(artifact_id)
        return {**preview, "preview_digest": self._delete_preview_digest(preview)}

    def commit_artifact_delete(self, artifact_id: str, preview_digest: str) -> dict[str, Any]:
        if not isinstance(preview_digest, str) or not _HEX_DIGEST_RE.fullmatch(preview_digest):
            _raise("ARTIFACT_DELETE_PREVIEW_INVALID", "artifact deletion preview digest is invalid")
        current = self._registry.preview_delete(artifact_id)
        current_digest = self._delete_preview_digest(current)
        if current_digest != preview_digest.lower():
            _raise("ARTIFACT_DELETE_PREVIEW_MISMATCH", "artifact deletion preview is stale or mismatched")
        deleted = self._registry.delete_previewed(current)
        projected = self._public_artifact_metadata(deleted)
        assert isinstance(projected, dict)
        return projected

    @staticmethod
    def _job_binding_digest(record: Mapping[str, Any]) -> str:
        fields = (
            "job_id",
            "kind",
            "operation_id",
            "artifact_id",
            "artifact_kind",
            "artifact_sha256",
            "artifact_size_bytes",
        )
        identity = {field: record[field] for field in fields if field in record}
        if "private_inputs" in record:
            identity["private_inputs"] = record["private_inputs"]
        return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()

    @staticmethod
    def _restore_preview_digest(preview: Mapping[str, Any]) -> str:
        return hashlib.sha256(canonical_json(dict(preview)).encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_export_selection(selection: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(selection, Mapping):
            _raise("LOGICAL_EXPORT_SELECTION_INVALID", "logical export selection must be an object")
        selection_copy = dict(selection)
        required = {
            "scope",
            "project_id",
            "worktree_id",
            "workflow_id",
            "agent_id",
            "lifecycle_states",
            "max_records",
        }
        if set(selection_copy) != required:
            _raise("LOGICAL_EXPORT_SELECTION_INVALID", "logical export selection fields are not exact")
        try:
            scope = validate_scope(
                selection_copy["scope"],
                selection_copy["project_id"],
                selection_copy["worktree_id"],
                selection_copy["workflow_id"],
                selection_copy["agent_id"],
            ).to_dict()
        except AdapterError as exc:
            raise AdapterError("LOGICAL_EXPORT_SELECTION_INVALID", "logical export scope identity is invalid") from exc
        lifecycle_states = selection_copy["lifecycle_states"]
        if (
            not isinstance(lifecycle_states, list)
            or not lifecycle_states
            or len(lifecycle_states) > len(LIFECYCLE_STATES)
            or any(not isinstance(item, str) or item not in LIFECYCLE_STATES for item in lifecycle_states)
            or len(set(lifecycle_states)) != len(lifecycle_states)
        ):
            _raise("LOGICAL_EXPORT_SELECTION_INVALID", "logical export lifecycle_states are invalid")
        max_records = selection_copy["max_records"]
        if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 1000:
            _raise("LOGICAL_EXPORT_SELECTION_INVALID", "logical export max_records must be between 1 and 1000")
        selection_copy = {
            **scope,
            "lifecycle_states": list(lifecycle_states),
            "max_records": max_records,
        }
        try:
            selection_size = len(canonical_json(selection_copy).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise AdapterError("LOGICAL_EXPORT_SELECTION_INVALID", "logical export selection is not canonical JSON") from exc
        if selection_size > 64 * 1024:
            _raise("LOGICAL_EXPORT_SELECTION_INVALID", "logical export selection is too large")
        return selection_copy

    @classmethod
    def _validate_private_inputs(cls, record: Mapping[str, Any]) -> None:
        private = record.get("private_inputs")
        kind = record.get("kind")
        if private is None:
            if kind == "LOGICAL_EXPORT":
                _raise("JOB_LEDGER_INVALID", "logical export job is missing durable private inputs")
            if kind in {"RESTORE_PREVIEW", "RESTORE_COMMIT"}:
                _raise("JOB_LEDGER_INVALID", "restore job is missing durable private inputs")
            return
        if not isinstance(private, dict):
            _raise("JOB_LEDGER_INVALID", "database job private inputs are malformed")
        try:
            encoded_size = len(canonical_json(private).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise AdapterError("JOB_LEDGER_INVALID", "database job private inputs are not canonical JSON") from exc
        if encoded_size > 128 * 1024:
            _raise("JOB_LEDGER_INVALID", "database job private inputs exceed their bounded size")

        if kind == "LOGICAL_EXPORT":
            if set(private) != {"selection", "producer"}:
                _raise("JOB_LEDGER_INVALID", "logical export private inputs fields are not exact")
            cls._normalize_export_selection(private["selection"])
            cls._normalize_logical_export_producer(private["producer"])
            return

        if kind == "RESTORE_PREVIEW":
            allowed_sets = (
                {"trusted_origin_attested"},
                {"trusted_origin_attested", "preview", "preview_digest"},
            )
            if set(private) not in allowed_sets or not isinstance(private.get("trusted_origin_attested"), bool):
                _raise("JOB_LEDGER_INVALID", "restore preview private inputs fields are invalid")
            if "preview" in private:
                preview = cls._safe_job_result(private["preview"])
                digest = private.get("preview_digest")
                if (
                    not isinstance(digest, str)
                    or not _HEX_DIGEST_RE.fullmatch(digest)
                    or digest.lower() != cls._restore_preview_digest(preview)
                ):
                    _raise("JOB_LEDGER_INVALID", "restore preview digest binding is invalid")
            return

        if kind == "RESTORE_COMMIT":
            if set(private) != {
                "trusted_origin_attested",
                "preview",
                "preview_digest",
                "preview_job_id",
                "state_restoration_acknowledged",
            }:
                _raise("JOB_LEDGER_INVALID", "restore commit private inputs fields are not exact")
            if private.get("trusted_origin_attested") is not True:
                _raise("JOB_LEDGER_INVALID", "restore commit lost trusted-origin attestation")
            if private.get("state_restoration_acknowledged") is not True:
                _raise("JOB_LEDGER_INVALID", "restore commit lost state-restoration acknowledgement")
            preview_job_id = private.get("preview_job_id")
            if not isinstance(preview_job_id, str):
                _raise("JOB_LEDGER_INVALID", "restore commit preview job identity is invalid")
            _canonical_uuid(preview_job_id, "preview_job_id")
            preview = cls._safe_job_result(private.get("preview"))
            digest = private.get("preview_digest")
            if (
                not isinstance(digest, str)
                or not _HEX_DIGEST_RE.fullmatch(digest)
                or digest.lower() != cls._restore_preview_digest(preview)
            ):
                _raise("JOB_LEDGER_INVALID", "restore commit preview digest binding is invalid")
            return

        _raise("JOB_LEDGER_INVALID", "database job kind does not permit private inputs")

    @staticmethod
    def _public_job_record(record: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "private_inputs"}

    def _load_jobs(self) -> dict[str, dict[str, Any]]:
        path = self._job_ledger_path
        if not path.exists() and not path.is_symlink():
            return {}
        try:
            info = _validate_private_regular_file(
                path,
                code="JOB_LEDGER_INVALID",
                message="database job ledger is not an owner-controlled regular file",
            )
        except FileNotFoundError:
            return {}
        if info.st_size > _JOB_LEDGER_MAX_BYTES:
            _raise("JOB_LEDGER_INVALID", "database job ledger exceeds its bounded size")
        if os.name != "nt":
            os.chmod(path, 0o600)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("JOB_LEDGER_INVALID", "database job ledger cannot be read") from exc
        if not isinstance(value, dict):
            _raise("JOB_LEDGER_INVALID", "database job ledger is malformed")
        jobs: dict[str, dict[str, Any]] = {}
        for job_id, record in value.items():
            if not isinstance(job_id, str) or not isinstance(record, dict):
                _raise("JOB_LEDGER_INVALID", "database job ledger contains malformed entries")
            _canonical_uuid(job_id, "job_id")
            if record.get("job_id") != job_id:
                _raise("JOB_LEDGER_INVALID", "database job ledger identity is inconsistent")
            jobs[job_id] = self._validate_loaded_job(record)
        return jobs

    def _validate_loaded_job(self, record: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "job_id",
            "kind",
            "operation_id",
            "phase",
            "created_at",
            "updated_at",
            "artifact_id",
            "artifact_kind",
            "artifact_sha256",
            "artifact_size_bytes",
            "binding_digest",
            "private_inputs",
            "result",
            "error",
        }
        if set(record) - allowed:
            _raise("JOB_LEDGER_INVALID", "database job ledger contains unsupported fields")
        job_id = record.get("job_id")
        operation_id = record.get("operation_id")
        if not isinstance(job_id, str) or not isinstance(operation_id, str):
            _raise("JOB_LEDGER_INVALID", "database job ledger identity fields are invalid")
        _canonical_uuid(job_id, "job_id")
        _canonical_uuid(operation_id, "operation_id")
        kind = record.get("kind")
        phase = record.get("phase")
        if kind not in _JOB_KINDS or phase not in _JOB_PHASES:
            _raise("JOB_LEDGER_INVALID", "database job ledger kind or phase is invalid")
        for field in ("created_at", "updated_at"):
            value = record.get(field)
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128:
                _raise("JOB_LEDGER_INVALID", "database job ledger timestamp is invalid")
        artifact_id = record.get("artifact_id")
        if artifact_id is not None and (
            not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(artifact_id)
        ):
            _raise("JOB_LEDGER_INVALID", "database job ledger artifact identity is invalid")
        artifact_kind = record.get("artifact_kind")
        if artifact_kind is not None and (
            not isinstance(artifact_kind, str)
            or not artifact_kind
            or len(artifact_kind.encode("utf-8")) > 64
        ):
            _raise("JOB_LEDGER_INVALID", "database job ledger artifact kind is invalid")
        artifact_sha256 = record.get("artifact_sha256")
        if artifact_sha256 is not None and (
            not isinstance(artifact_sha256, str) or not _HEX_DIGEST_RE.fullmatch(artifact_sha256)
        ):
            _raise("JOB_LEDGER_INVALID", "database job ledger artifact checksum is invalid")
        artifact_size = record.get("artifact_size_bytes")
        if artifact_size is not None and (
            isinstance(artifact_size, bool) or not isinstance(artifact_size, int) or artifact_size < 0
        ):
            _raise("JOB_LEDGER_INVALID", "database job ledger artifact size is invalid")
        self._validate_private_inputs(record)
        binding_digest = record.get("binding_digest")
        if (
            not isinstance(binding_digest, str)
            or not _HEX_DIGEST_RE.fullmatch(binding_digest)
            or binding_digest.lower() != self._job_binding_digest(record)
        ):
            _raise("JOB_LEDGER_INVALID", "database job ledger binding digest is invalid")
        if "result" in record:
            if phase != "SUCCEEDED":
                _raise("JOB_LEDGER_INVALID", "database job ledger has a result in a non-success phase")
            self._safe_job_result(record["result"])
        if "error" in record:
            error = record["error"]
            if (
                phase != "FAILED"
                or not isinstance(error, dict)
                or set(error) != {"code", "message"}
                or not isinstance(error.get("code"), str)
                or not _JOB_ERROR_CODE_RE.fullmatch(error["code"])
                or error.get("message") != "database job failed"
            ):
                _raise("JOB_LEDGER_INVALID", "database job ledger error receipt is invalid")
        if phase == "SUCCEEDED" and "result" not in record:
            _raise("JOB_LEDGER_INVALID", "successful database job ledger entry is missing its result")
        if phase == "FAILED" and "error" not in record:
            _raise("JOB_LEDGER_INVALID", "failed database job ledger entry is missing its error")
        if phase in {"QUEUED", "RUNNING"} and ("result" in record or "error" in record):
            _raise("JOB_LEDGER_INVALID", "unfinished database job ledger entry contains a terminal receipt")
        if kind == "RESTORE_PREVIEW" and phase == "SUCCEEDED":
            private = record.get("private_inputs")
            if not isinstance(private, dict) or private.get("preview") != record.get("result"):
                _raise("JOB_LEDGER_INVALID", "succeeded restore preview result is not bound to its private preview")
        return dict(record)

    def _reconcile_interrupted_jobs(self) -> None:
        with self._job_lock:
            jobs = self._load_jobs()
            changed = False
            for job_id, record in list(jobs.items()):
                if record.get("phase") != "RUNNING":
                    continue
                reconciled = dict(record)
                reconciled["phase"] = "FAILED"
                reconciled["updated_at"] = iso_now()
                reconciled["error"] = {"code": "JOB_INTERRUPTED", "message": "database job failed"}
                reconciled.pop("result", None)
                reconciled["binding_digest"] = self._job_binding_digest(reconciled)
                jobs[job_id] = reconciled
                changed = True
            if changed:
                self._save_jobs(jobs)

    def _save_jobs(self, jobs: dict[str, dict[str, Any]]) -> None:
        raw = (canonical_json(jobs) + "\n").encode("utf-8")
        if len(raw) > _JOB_LEDGER_MAX_BYTES:
            _raise("JOB_LEDGER_LIMIT", "database job ledger exceeds its bounded size")
        stage = self._registry.root / f".{_JOB_LEDGER_NAME}.{self._new_uuid('job_ledger_write_id')}.tmp"
        flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd, parent_guard = _open_new_private_file(
            stage,
            flags,
            code="JOB_LEDGER_INVALID",
            message="database job ledger staging file is not owner-controlled",
        )
        fd_open = True
        try:
            _write_private_bytes(fd, raw)
            if os.name != "nt":
                os.close(fd)
                fd_open = False
            if self._job_ledger_path.exists() or self._job_ledger_path.is_symlink():
                _validate_private_regular_file(
                    self._job_ledger_path,
                    code="JOB_LEDGER_INVALID",
                    message="database job ledger changed to an unsafe path",
                )
            if parent_guard is not None:
                parent_guard.require_current()
            _replace_private_file(
                stage,
                self._job_ledger_path,
                source_fd=fd if os.name == "nt" else None,
                parent_guard=parent_guard,
            )
            _protect_created_private_file(
                self._job_ledger_path,
                code="JOB_LEDGER_INVALID",
                message="database job ledger is not owner-controlled",
            )
            _fsync_directory(self._registry.root)
        finally:
            if fd_open:
                try:
                    os.close(fd)
                except OSError:
                    pass
            _unlink_private_file(stage, parent_guard=parent_guard)
            if parent_guard is not None:
                parent_guard.close()

    def _new_operation_id(self, operation_id: str | None) -> str:
        if operation_id is None:
            return self._new_uuid("operation_id")
        return _canonical_uuid(operation_id, "operation_id")

    def _create_job(
        self,
        *,
        kind: str,
        operation_id: str | None = None,
        artifact: dict[str, Any] | None = None,
        planned_artifact_id: str | None = None,
        private_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_id = self._new_uuid("job_id")
        operation = self._new_operation_id(operation_id)
        now = iso_now()
        record: dict[str, Any] = {
            "job_id": job_id,
            "kind": kind,
            "operation_id": operation,
            "phase": "QUEUED",
            "created_at": now,
            "updated_at": now,
        }
        if artifact is not None:
            record.update(
                {
                    "artifact_id": artifact["artifact_id"],
                    "artifact_kind": artifact["kind"],
                    "artifact_sha256": artifact["sha256"],
                    "artifact_size_bytes": artifact["size_bytes"],
                }
            )
        elif planned_artifact_id is not None:
            record["artifact_id"] = planned_artifact_id
        if private_inputs is not None:
            record["private_inputs"] = dict(private_inputs)
        self._validate_private_inputs(record)
        record["binding_digest"] = self._job_binding_digest(record)
        with self._job_lock:
            jobs = self._load_jobs()
            if job_id in jobs:
                _raise("JOB_ALREADY_EXISTS", "server-generated job identity collided")
            jobs[job_id] = record
            self._save_jobs(jobs)
        return self._public_job_record(record)

    def _get_job(self, job_id: str) -> dict[str, Any]:
        job_id = _canonical_uuid(job_id, "job_id")
        with self._job_lock:
            record = self._load_jobs().get(job_id)
        if record is None:
            _raise("JOB_NOT_FOUND", "database job was not found")
        return dict(record)

    def job_status(self, job_id: str) -> dict[str, Any]:
        return self._public_job_record(self._get_job(job_id))

    def _update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._job_lock:
            jobs = self._load_jobs()
            record = jobs.get(job_id)
            if record is None:
                _raise("JOB_NOT_FOUND", "database job was not found")
            updated = dict(record)
            updated.update(changes)
            updated["updated_at"] = iso_now()
            updated["binding_digest"] = self._job_binding_digest(updated)
            self._validate_loaded_job(updated)
            jobs[job_id] = updated
            self._save_jobs(jobs)
        return dict(updated)

    def _transition_job_to_running(self, job_id: str) -> dict[str, Any]:
        with self._job_lock:
            jobs = self._load_jobs()
            record = jobs.get(job_id)
            if record is None:
                _raise("JOB_NOT_FOUND", "database job was not found")
            updated = dict(record)
            updated["phase"] = "RUNNING"
            updated["updated_at"] = iso_now()
            updated.pop("result", None)
            updated.pop("error", None)
            updated["binding_digest"] = self._job_binding_digest(updated)
            self._validate_loaded_job(updated)
            jobs[job_id] = updated
            self._save_jobs(jobs)
        return updated

    @classmethod
    def _validate_safe_job_value(cls, value: Any, *, depth: int = 0) -> Any:
        if depth > 8:
            _raise("JOB_RESULT_UNSAFE", "database job result nesting is too deep")
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if value != value or value in {float("inf"), float("-inf")}:
                _raise("JOB_RESULT_UNSAFE", "database job result contains a non-finite number")
            return value
        if isinstance(value, str):
            if len(value.encode("utf-8")) > 16 * 1024:
                _raise("JOB_RESULT_UNSAFE", "database job result contains an oversized string")
            return value
        if isinstance(value, list):
            if len(value) > 1000:
                _raise("JOB_RESULT_UNSAFE", "database job result contains an oversized list")
            return [cls._validate_safe_job_value(item, depth=depth + 1) for item in value]
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    _raise("JOB_RESULT_UNSAFE", "database job result contains an invalid key")
                normalized = key.casefold()
                if (
                    normalized in _UNSAFE_JOB_RESULT_KEYS
                    or normalized.endswith("_secret")
                    or normalized.endswith("_token")
                    or normalized.endswith("_cookie")
                    or normalized.endswith("_path")
                ):
                    _raise("JOB_RESULT_UNSAFE", "database job result contains content or credential material")
                result[key] = cls._validate_safe_job_value(item, depth=depth + 1)
            return result
        _raise("JOB_RESULT_UNSAFE", "database job result contains an unsupported value")

    @classmethod
    def _safe_job_result(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            _raise("JOB_RESULT_UNSAFE", "database job hook must return a safe result object")
        result = cls._validate_safe_job_value(value)
        assert isinstance(result, dict)
        if len(canonical_json(result).encode("utf-8")) > _JOB_RESULT_MAX_BYTES:
            _raise("JOB_RESULT_UNSAFE", "database job result exceeds its bounded size")
        return result

    @staticmethod
    def _restore_safe_string(value: Any, field: str, *, max_bytes: int = 256) -> str:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > max_bytes:
            _raise("JOB_RESULT_UNSAFE", f"restore result {field} is invalid")
        return value

    @classmethod
    def _restore_safe_ids(cls, value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 64:
            _raise("JOB_RESULT_UNSAFE", f"restore result {field} is invalid")
        result = [cls._restore_safe_string(item, field, max_bytes=128) for item in value]
        if len(set(result)) != len(result):
            _raise("JOB_RESULT_UNSAFE", f"restore result {field} contains duplicate identities")
        return result

    @classmethod
    def _restore_safe_mapping(cls, value: Any, field: str) -> dict[str, str]:
        if not isinstance(value, dict) or len(value) > 64:
            _raise("JOB_RESULT_UNSAFE", f"restore result {field} is invalid")
        result: dict[str, str] = {}
        for key, item in value.items():
            safe_key = cls._restore_safe_string(key, field, max_bytes=128)
            result[safe_key] = cls._restore_safe_string(item, field, max_bytes=128)
        return result

    @staticmethod
    def _restore_safe_nonnegative_int(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _raise("JOB_RESULT_UNSAFE", f"restore result {field} is invalid")
        return value

    @classmethod
    def _restore_product_proof_summary(
        cls,
        proof: Any,
        *,
        operation_id: str | None,
        generation: int | None,
        ready_instance_ids: list[str] | None,
        reconnect_failures: dict[str, str] | None,
    ) -> tuple[dict[str, Any], str, int, int, int, list[str], dict[str, str]]:
        if not isinstance(proof, dict) or set(proof) != _RESTORE_PRODUCT_PROOF_KEYS:
            _raise("JOB_RESULT_UNSAFE", "restore post-activation verification has an unsupported shape")
        status = cls._restore_safe_string(proof.get("status"), "post-activation status", max_bytes=64)
        if status != "VERIFIED" or proof.get("bounded_read_verified") is not True:
            _raise("JOB_RESULT_UNSAFE", "restore post-activation verification is not VERIFIED")
        proof_operation_id = _canonical_uuid(
            cls._restore_safe_string(proof.get("operation_id"), "post-activation operation_id", max_bytes=64),
            "operation_id",
        )
        proof_generation = cls._restore_safe_nonnegative_int(
            proof.get("generation"),
            "post-activation generation",
        )
        if operation_id is not None and proof_operation_id != operation_id:
            _raise("JOB_RESULT_UNSAFE", "restore post-activation operation identity is inconsistent")
        if generation is not None and proof_generation != generation:
            _raise("JOB_RESULT_UNSAFE", "restore post-activation generation is inconsistent")

        database_path = cls._restore_safe_string(
            proof.get("database_path"),
            "post-activation database path",
            max_bytes=4096,
        )
        database_device = cls._restore_safe_nonnegative_int(
            proof.get("database_device"),
            "post-activation database device",
        )
        database_inode = cls._restore_safe_nonnegative_int(
            proof.get("database_inode"),
            "post-activation database inode",
        )
        storage_contract_version = cls._restore_safe_nonnegative_int(
            proof.get("storage_contract_version"),
            "post-activation storage contract version",
        )
        embedding_profile = cls._validate_safe_job_value(proof.get("embedding_profile"), depth=1)
        if not isinstance(embedding_profile, dict):
            _raise("JOB_RESULT_UNSAFE", "restore embedding profile is invalid")
        if len(canonical_json(embedding_profile).encode("utf-8")) > 4096:
            _raise("JOB_RESULT_UNSAFE", "restore embedding profile exceeds its bounded size")

        proof_ready = cls._restore_safe_ids(proof.get("ready_instance_ids"), "ready_instance_ids")
        proof_failures = cls._restore_safe_mapping(proof.get("reconnect_failures"), "reconnect_failures")
        if ready_instance_ids is not None and proof_ready != ready_instance_ids:
            _raise("JOB_RESULT_UNSAFE", "restore post-activation READY identities are inconsistent")
        if reconnect_failures is not None and proof_failures != reconnect_failures:
            _raise("JOB_RESULT_UNSAFE", "restore post-activation reconnect failures are inconsistent")

        evidence = proof.get("resumed_runtime_evidence")
        if not isinstance(evidence, dict) or len(evidence) > 64 or set(evidence) != set(proof_ready):
            _raise("JOB_RESULT_UNSAFE", "restore resumed runtime evidence identities are invalid")
        for instance_id in proof_ready:
            item = evidence.get(instance_id)
            if not isinstance(item, dict) or set(item) != _RESTORE_RUNTIME_EVIDENCE_KEYS:
                _raise("JOB_RESULT_UNSAFE", "restore resumed runtime evidence has an unsupported shape")
            if cls._restore_safe_string(item.get("instance_id"), "runtime instance_id", max_bytes=128) != instance_id:
                _raise("JOB_RESULT_UNSAFE", "restore resumed runtime identity is inconsistent")
            for field in (
                "protocol_version",
                "owner_uid",
                "pid",
                "process_start_ticks",
                "device",
                "inode",
                "generation",
            ):
                cls._restore_safe_nonnegative_int(item.get(field), f"runtime {field}")
            if (
                cls._restore_safe_string(item.get("database_path"), "runtime database path", max_bytes=4096)
                != database_path
                or item.get("device") != database_device
                or item.get("inode") != database_inode
                or item.get("generation") != proof_generation
                or cls._restore_safe_string(item.get("lifecycle_state"), "runtime lifecycle_state", max_bytes=64)
                != "OPEN"
                or item.get("lease_open") is not True
            ):
                _raise("JOB_RESULT_UNSAFE", "restore resumed runtime evidence is inconsistent")
            cls._restore_safe_string(item.get("boot_id"), "runtime boot_id", max_bytes=256)
            cls._restore_safe_string(
                item.get("control_socket_path"),
                "runtime control socket path",
                max_bytes=4096,
            )

        summary = {
            "status": "VERIFIED",
            "bounded_read_verified": True,
            "storage_contract_version": storage_contract_version,
            "embedding_profile": embedding_profile,
            "resumed_runtime_count": len(proof_ready),
            "resumed_runtime_instance_ids": list(proof_ready),
            "reconnect_failure_count": len(proof_failures),
            "reconnect_failure_instance_ids": sorted(proof_failures),
        }
        return (
            summary,
            proof_operation_id,
            proof_generation,
            database_device,
            database_inode,
            proof_ready,
            proof_failures,
        )

    @classmethod
    def _restore_public_result(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            _raise("JOB_RESULT_UNSAFE", "restore hook must return an object")
        unknown = set(value) - _RESTORE_PUBLIC_RESULT_KEYS
        if unknown:
            _raise("JOB_RESULT_UNSAFE", "restore result contains unsupported fields")

        result: dict[str, Any] = {}
        if "status" in value:
            result["status"] = cls._restore_safe_string(value["status"], "status", max_bytes=64)
        action: str | None = None
        if "action" in value:
            action = cls._restore_safe_string(value["action"], "action", max_bytes=128)
        if "operation" in value:
            operation = cls._restore_safe_string(value["operation"], "operation", max_bytes=128)
            if action is not None and action != operation:
                _raise("JOB_RESULT_UNSAFE", "restore result action is inconsistent")
            action = operation
        if action is not None:
            result["action"] = action

        operation_id: str | None = None
        if "operation_id" in value:
            operation_id = _canonical_uuid(
                cls._restore_safe_string(value["operation_id"], "operation_id", max_bytes=64),
                "operation_id",
            )
            result["operation_id"] = operation_id
        generation: int | None = None
        if "generation" in value:
            generation = cls._restore_safe_nonnegative_int(value["generation"], "generation")
            result["generation"] = generation

        if "artifact_id" in value:
            artifact_id = cls._restore_safe_string(value["artifact_id"], "artifact_id", max_bytes=128)
            if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
                _raise("JOB_RESULT_UNSAFE", "restore result artifact_id is invalid")
        if "artifact_sha256" in value:
            artifact_sha256 = cls._restore_safe_string(value["artifact_sha256"], "artifact_sha256", max_bytes=64)
            if not _HEX_DIGEST_RE.fullmatch(artifact_sha256):
                _raise("JOB_RESULT_UNSAFE", "restore result artifact_sha256 is invalid")
        if "pre_restore_artifact_id" in value:
            pre_restore_artifact_id = cls._restore_safe_string(
                value["pre_restore_artifact_id"],
                "pre_restore_artifact_id",
                max_bytes=128,
            )
            if not _ARTIFACT_ID_RE.fullmatch(pre_restore_artifact_id):
                _raise("JOB_RESULT_UNSAFE", "restore result pre_restore_artifact_id is invalid")
            result["pre_restore_artifact_id"] = pre_restore_artifact_id

        ready_instance_ids: list[str] | None = None
        if "ready_instance_ids" in value:
            ready_instance_ids = cls._restore_safe_ids(value["ready_instance_ids"], "ready_instance_ids")
            result["ready_instance_ids"] = ready_instance_ids
        reconnect_failures: dict[str, str] | None = None
        if "reconnect_failures" in value:
            reconnect_failures = cls._restore_safe_mapping(value["reconnect_failures"], "reconnect_failures")
            result["reconnect_failures"] = reconnect_failures
        if "maintenance_participant_failures" in value:
            result["maintenance_participant_failures"] = cls._restore_safe_mapping(
                value["maintenance_participant_failures"],
                "maintenance_participant_failures",
            )
        if "idempotent_replay" in value:
            if not isinstance(value["idempotent_replay"], bool):
                _raise("JOB_RESULT_UNSAFE", "restore result idempotent_replay is invalid")
            result["idempotent_replay"] = value["idempotent_replay"]

        activation_device: int | None = None
        activation_inode: int | None = None
        if "activation_device" in value:
            activation_device = cls._restore_safe_nonnegative_int(value["activation_device"], "activation_device")
        if "activation_inode" in value:
            activation_inode = cls._restore_safe_nonnegative_int(value["activation_inode"], "activation_inode")

        if "post_activation_verification" in value:
            (
                summary,
                proof_operation_id,
                proof_generation,
                proof_device,
                proof_inode,
                proof_ready,
                proof_failures,
            ) = cls._restore_product_proof_summary(
                value["post_activation_verification"],
                operation_id=operation_id,
                generation=generation,
                ready_instance_ids=ready_instance_ids,
                reconnect_failures=reconnect_failures,
            )
            if operation_id is None:
                operation_id = proof_operation_id
                result["operation_id"] = proof_operation_id
            if generation is None:
                generation = proof_generation
                result["generation"] = proof_generation
            if ready_instance_ids is None:
                result["ready_instance_ids"] = proof_ready
            if reconnect_failures is None:
                result["reconnect_failures"] = proof_failures
            if activation_device is not None and activation_device != proof_device:
                _raise("JOB_RESULT_UNSAFE", "restore activation device identity is inconsistent")
            if activation_inode is not None and activation_inode != proof_inode:
                _raise("JOB_RESULT_UNSAFE", "restore activation inode identity is inconsistent")
            activation_device = proof_device
            activation_inode = proof_inode
            result["post_activation_verification"] = summary

        if activation_device is not None:
            result["activation_device"] = activation_device
        if activation_inode is not None:
            result["activation_inode"] = activation_inode
        return cls._safe_job_result(result)

    def _artifact_binding(self, job: dict[str, Any], expected_kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
        artifact_id = job.get("artifact_id")
        if not isinstance(artifact_id, str):
            _raise("JOB_BINDING_INVALID", "database job is missing its artifact identity")
        metadata = self._registry.metadata(artifact_id)
        if metadata.get("kind") != expected_kind:
            _raise("JOB_BINDING_INVALID", "database job artifact kind changed or is unsupported")
        for field, key in (
            ("artifact_sha256", "sha256"),
            ("artifact_size_bytes", "size_bytes"),
            ("artifact_kind", "kind"),
        ):
            if job.get(field) != metadata.get(key):
                _raise("JOB_BINDING_CHANGED", "database job artifact binding changed before execution")
        binding = {
            "job_id": job["job_id"],
            "kind": job["kind"],
            "operation_id": job["operation_id"],
            "artifact_id": metadata["artifact_id"],
            "artifact_kind": metadata["kind"],
            "artifact_sha256": metadata["sha256"],
            "artifact_size_bytes": metadata["size_bytes"],
            "binding_digest": job["binding_digest"],
        }
        return metadata, binding

    def _read_logical_artifact(self, artifact_id: str) -> bytes:
        payload = bytearray()
        with self.open_download(artifact_id) as handle:
            if handle.kind != "LOGICAL_IMPORT" or handle.size_bytes > LOGICAL_IMPORT_MAX_BYTES:
                _raise("JOB_BINDING_INVALID", "logical import job requires a bounded LOGICAL_IMPORT artifact")
            while True:
                chunk = handle.read(DOWNLOAD_READ_MAX_BYTES)
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > LOGICAL_IMPORT_MAX_BYTES:
                    _raise("UPLOAD_TOO_LARGE", "logical import exceeds the 10 MiB limit")
        return bytes(payload)

    def begin_backup_job(self, *, operation_id: str | None = None) -> dict[str, Any]:
        if self._active_db_path is None:
            _raise("BACKUP_NOT_CONFIGURED", "backup creation requires a trusted active database path")
        artifact_id = self._new_uuid("artifact_id")
        return self._create_job(
            kind="BACKUP_CREATE",
            operation_id=operation_id,
            planned_artifact_id=artifact_id,
        )

    def begin_logical_import_preview_job(
        self,
        artifact_id: str,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        if self._logical_import_preview is None:
            _raise("LOGICAL_IMPORT_PREVIEW_UNAVAILABLE", "logical import preview hook is not configured")
        artifact = self._registry.metadata(artifact_id)
        if artifact.get("kind") != "LOGICAL_IMPORT":
            _raise("JOB_BINDING_INVALID", "logical import preview requires a LOGICAL_IMPORT artifact")
        return self._create_job(kind="LOGICAL_IMPORT_PREVIEW", operation_id=operation_id, artifact=artifact)

    def begin_logical_import_commit_job(
        self,
        artifact_id: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        if self._logical_import_apply is None:
            _raise("LOGICAL_IMPORT_APPLY_UNAVAILABLE", "logical import apply hook is not configured")
        artifact = self._registry.metadata(artifact_id)
        if artifact.get("kind") != "LOGICAL_IMPORT":
            _raise("JOB_BINDING_INVALID", "logical import commit requires a LOGICAL_IMPORT artifact")
        return self._create_job(kind="LOGICAL_IMPORT_COMMIT", operation_id=operation_id, artifact=artifact)

    def begin_restore_preview_job(
        self,
        artifact_id: str,
        *,
        operation_id: str | None = None,
        trusted_origin_attested: bool = False,
    ) -> dict[str, Any]:
        if self._restore_preview is None:
            _raise("RESTORE_PREVIEW_UNAVAILABLE", "restore preview hook is not configured")
        if not isinstance(trusted_origin_attested, bool):
            _raise("RESTORE_TRUST_INVALID", "trusted-origin attestation must be a boolean")
        artifact = self._registry.metadata(artifact_id)
        if artifact.get("kind") not in {"RESTORE_UPLOAD", "SQLITE_BACKUP"}:
            _raise("JOB_BINDING_INVALID", "restore preview requires a managed restore upload")
        if (
            artifact.get("kind") == "SQLITE_BACKUP"
            and artifact.get("promoted_from_kind") != "RESTORE_UPLOAD"
            and artifact.get("local_database_registration") is not True
        ):
            _raise("JOB_BINDING_INVALID", "restore preview requires a promoted restore upload")
        return self._create_job(
            kind="RESTORE_PREVIEW",
            operation_id=operation_id,
            artifact=artifact,
            private_inputs={"trusted_origin_attested": trusted_origin_attested},
        )

    def begin_database_switch_preview_job(
        self,
        artifact_id: str,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        artifact = self._registry.metadata(artifact_id)
        if artifact.get("kind") != "SQLITE_BACKUP" or artifact.get("local_database_registration") is not True:
            _raise("DATABASE_NOT_REGISTERED", "database switch requires a trusted local registration")
        return self.begin_restore_preview_job(
            artifact_id,
            operation_id=operation_id,
            trusted_origin_attested=True,
        )

    def begin_restore_commit_job(
        self,
        artifact_id: str,
        *,
        operation_id: str,
        state_restoration_acknowledged: bool,
    ) -> dict[str, Any]:
        if self._restore_apply is None:
            _raise("RESTORE_APPLY_UNAVAILABLE", "restore apply hook is not configured")
        if state_restoration_acknowledged is not True:
            _raise("RESTORE_ACK_REQUIRED", "restore commit requires explicit state-restoration acknowledgement")
        operation = _canonical_uuid(operation_id, "operation_id")
        artifact = self._registry.metadata(artifact_id)
        if artifact.get("kind") != "SQLITE_BACKUP" or (
            artifact.get("promoted_from_kind") != "RESTORE_UPLOAD"
            and artifact.get("local_database_registration") is not True
        ):
            _raise("JOB_BINDING_INVALID", "restore commit requires the promoted SQLITE_BACKUP artifact")
        with self._job_lock:
            jobs = self._load_jobs()
        previews = [
            record
            for record in jobs.values()
            if record.get("kind") == "RESTORE_PREVIEW"
            and record.get("phase") == "SUCCEEDED"
            and record.get("operation_id") == operation
            and record.get("artifact_id") == artifact_id
        ]
        if not previews:
            _raise("RESTORE_PREVIEW_REQUIRED", "restore commit requires a succeeded server-side preview")
        if len(previews) != 1:
            _raise("RESTORE_PREVIEW_AMBIGUOUS", "restore commit has multiple succeeded previews for the same operation")
        preview_job = previews[0]
        for field, key in (
            ("artifact_kind", "kind"),
            ("artifact_sha256", "sha256"),
            ("artifact_size_bytes", "size_bytes"),
        ):
            if preview_job.get(field) != artifact.get(key):
                _raise("JOB_BINDING_CHANGED", "restore preview artifact binding changed before commit creation")
        private = preview_job.get("private_inputs")
        if not isinstance(private, dict) or set(private) != {
            "trusted_origin_attested",
            "preview",
            "preview_digest",
        }:
            _raise("RESTORE_PREVIEW_INVALID", "succeeded restore preview lost its private binding")
        exact_preview = self._safe_job_result(private["preview"])
        preview_digest = private.get("preview_digest")
        if (
            private.get("trusted_origin_attested") is not True
            or not isinstance(preview_digest, str)
            or preview_digest != self._restore_preview_digest(exact_preview)
            or preview_job.get("result") != exact_preview
        ):
            _raise("RESTORE_PREVIEW_INVALID", "succeeded restore preview binding is stale or mismatched")
        return self._create_job(
            kind="RESTORE_COMMIT",
            operation_id=operation,
            artifact=artifact,
            private_inputs={
                "trusted_origin_attested": True,
                "preview": exact_preview,
                "preview_digest": preview_digest,
                "preview_job_id": preview_job["job_id"],
                "state_restoration_acknowledged": True,
            },
        )

    def begin_database_switch_commit_job(
        self,
        artifact_id: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        artifact = self._registry.metadata(artifact_id)
        if artifact.get("kind") != "SQLITE_BACKUP" or artifact.get("local_database_registration") is not True:
            _raise("DATABASE_NOT_REGISTERED", "database switch requires a trusted local registration")
        return self.begin_restore_commit_job(
            artifact_id,
            operation_id=operation_id,
            state_restoration_acknowledged=True,
        )

    def begin_logical_export_job(
        self,
        *,
        selection: Mapping[str, Any],
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        if self._logical_export_selector is None:
            _raise("LOGICAL_EXPORT_SELECTOR_UNAVAILABLE", "logical export selector is not configured")
        if self._logical_export_producer is None:
            _raise("LOGICAL_EXPORT_PRODUCER_UNAVAILABLE", "logical export producer metadata is not configured")
        selection_copy = self._normalize_export_selection(selection)
        artifact_id = self._new_uuid("artifact_id")
        return self._create_job(
            kind="LOGICAL_EXPORT",
            operation_id=operation_id,
            planned_artifact_id=artifact_id,
            private_inputs={
                "selection": selection_copy,
                "producer": dict(self._logical_export_producer),
            },
        )

    def _prepare_restore_preview_execution(self, job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        artifact_id = job.get("artifact_id")
        if not isinstance(artifact_id, str):
            _raise("JOB_BINDING_INVALID", "restore preview job is missing its artifact identity")
        promoted = self._registry.promote_restore_upload(artifact_id)
        if (
            promoted.get("artifact_id") != artifact_id
            or promoted.get("sha256") != job.get("artifact_sha256")
            or promoted.get("size_bytes") != job.get("artifact_size_bytes")
        ):
            _raise("JOB_BINDING_CHANGED", "restore artifact immutable identity changed during promotion")
        if job.get("artifact_kind") not in {"RESTORE_UPLOAD", "SQLITE_BACKUP"}:
            _raise("JOB_BINDING_INVALID", "restore preview job artifact kind is invalid")
        if job.get("artifact_kind") != "SQLITE_BACKUP":
            job = self._update_job(job["job_id"], artifact_kind="SQLITE_BACKUP")
        _metadata, binding = self._artifact_binding(job, "SQLITE_BACKUP")
        private = job.get("private_inputs")
        if not isinstance(private, dict) or not isinstance(private.get("trusted_origin_attested"), bool):
            _raise("JOB_INPUT_UNAVAILABLE", "restore preview trusted-origin binding is unavailable")
        binding["trusted_origin_attested"] = private["trusted_origin_attested"]
        return job, binding

    def _persist_restore_preview(self, job: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
        private = job.get("private_inputs")
        if not isinstance(private, dict):
            _raise("JOB_INPUT_UNAVAILABLE", "restore preview private inputs are unavailable")
        updated_private = {
            "trusted_origin_attested": private.get("trusted_origin_attested") is True,
            "preview": preview,
            "preview_digest": self._restore_preview_digest(preview),
        }
        return self._update_job(job["job_id"], private_inputs=updated_private)

    async def _execute_job(self, job: dict[str, Any]) -> dict[str, Any]:
        kind = job["kind"]
        if kind == "BACKUP_CREATE":
            if self._active_db_path is None:
                _raise("BACKUP_NOT_CONFIGURED", "backup creation requires a trusted active database path")
            await _maybe_await(
                self._backup_create(
                    self._active_db_path,
                    self._registry,
                    str(job["artifact_id"]),
                )
            )
            return self.artifact_metadata(str(job["artifact_id"]))

        if kind == "LOGICAL_EXPORT":
            if self._logical_export_selector is None:
                _raise("LOGICAL_EXPORT_SELECTOR_UNAVAILABLE", "logical export selector is not configured")
            inputs = job.get("private_inputs")
            if not isinstance(inputs, dict):
                _raise("JOB_INPUT_UNAVAILABLE", "logical export durable inputs are unavailable")
            producer = inputs.get("producer")
            if not isinstance(producer, dict):
                _raise("JOB_INPUT_UNAVAILABLE", "logical export producer metadata is unavailable")
            records = await _maybe_await(self._logical_export_selector(dict(inputs["selection"])))
            if not isinstance(records, list):
                _raise("LOGICAL_EXPORT_SELECTOR_INVALID", "logical export selector must return a record list")
            create_registered_logical_export(
                records,
                self._registry,
                factlane_version=producer["factlane_version"],
                storage_contract_version=producer["storage_contract_version"],
                embedding_profile_id=producer["embedding_profile_id"],
                artifact_id=str(job["artifact_id"]),
            )
            return self.artifact_metadata(str(job["artifact_id"]))

        if kind in {"LOGICAL_IMPORT_PREVIEW", "LOGICAL_IMPORT_COMMIT"}:
            _metadata, binding = self._artifact_binding(job, "LOGICAL_IMPORT")
            payload = self._read_logical_artifact(str(job["artifact_id"]))
            hook = self._logical_import_preview if kind == "LOGICAL_IMPORT_PREVIEW" else self._logical_import_apply
            if hook is None:
                _raise("JOB_HOOK_UNAVAILABLE", "logical import job hook is not configured")
            return self._safe_job_result(await _maybe_await(hook(payload, dict(binding))))

        if kind == "RESTORE_PREVIEW":
            if self._restore_preview is None:
                _raise("JOB_HOOK_UNAVAILABLE", "restore preview hook is not configured")
            job, binding = self._prepare_restore_preview_execution(job)
            preview = self._safe_job_result(await _maybe_await(self._restore_preview(dict(binding))))
            self._persist_restore_preview(job, preview)
            return preview

        if kind == "RESTORE_COMMIT":
            _raise("RESTORE_AUTH_REQUIRED", "restore commit requires runtime-only authorization")

        _raise("JOB_KIND_UNSUPPORTED", "database job kind is unsupported")

    def _restore_commit_execution_binding(
        self,
        job: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        _metadata, binding = self._artifact_binding(job, "SQLITE_BACKUP")
        private = job.get("private_inputs")
        if not isinstance(private, dict):
            _raise("JOB_INPUT_UNAVAILABLE", "restore commit private inputs are unavailable")
        preview = self._safe_job_result(private.get("preview"))
        preview_digest = private.get("preview_digest")
        preview_job_id = private.get("preview_job_id")
        if (
            private.get("trusted_origin_attested") is not True
            or private.get("state_restoration_acknowledged") is not True
            or not isinstance(preview_digest, str)
            or preview_digest != self._restore_preview_digest(preview)
            or not isinstance(preview_job_id, str)
        ):
            _raise("RESTORE_PREVIEW_INVALID", "restore commit private preview binding is invalid")
        preview_job = self._get_job(preview_job_id)
        if (
            preview_job.get("kind") != "RESTORE_PREVIEW"
            or preview_job.get("phase") != "SUCCEEDED"
            or preview_job.get("operation_id") != job.get("operation_id")
            or preview_job.get("artifact_id") != job.get("artifact_id")
            or preview_job.get("artifact_kind") != "SQLITE_BACKUP"
            or preview_job.get("artifact_sha256") != job.get("artifact_sha256")
            or preview_job.get("artifact_size_bytes") != job.get("artifact_size_bytes")
            or preview_job.get("result") != preview
        ):
            _raise("RESTORE_PREVIEW_INVALID", "restore commit no longer matches its succeeded preview job")
        preview_private = preview_job.get("private_inputs")
        if (
            not isinstance(preview_private, dict)
            or preview_private.get("preview") != preview
            or preview_private.get("preview_digest") != preview_digest
            or preview_private.get("trusted_origin_attested") is not True
        ):
            _raise("RESTORE_PREVIEW_INVALID", "restore commit preview binding changed before execution")
        binding.update(
            {
                "trusted_origin_attested": True,
                "preview_digest": preview_digest,
                "preview_job_id": preview_job_id,
                "state_restoration_acknowledged": True,
            }
        )
        return binding, preview

    async def run_job(self, job_id: str) -> dict[str, Any]:
        job_id = _canonical_uuid(job_id, "job_id")
        job = self._get_job(job_id)
        if job["kind"] == "RESTORE_COMMIT":
            _raise("RESTORE_AUTH_REQUIRED", "restore commit requires runtime-only authorization")
        if job["phase"] in _TERMINAL_JOB_PHASES:
            return self._public_job_record(job)
        if job["phase"] != "QUEUED":
            _raise("JOB_ALREADY_RUNNING", "database job is already running")
        job = self._transition_job_to_running(job_id)
        try:
            raw_result = await self._execute_job(job)
            result = self._safe_job_result(raw_result)
        except AdapterError as exc:
            code = (
                exc.code
                if isinstance(exc.code, str) and _JOB_ERROR_CODE_RE.fullmatch(exc.code)
                else "DATABASE_JOB_FAILED"
            )
            failed = self._update_job(
                job_id,
                phase="FAILED",
                error={"code": code, "message": "database job failed"},
            )
            return self._public_job_record(failed)
        except Exception:
            failed = self._update_job(
                job_id,
                phase="FAILED",
                error={"code": "DATABASE_JOB_FAILED", "message": "database job failed"},
            )
            return self._public_job_record(failed)
        succeeded = self._update_job(job_id, phase="SUCCEEDED", result=result)
        return self._public_job_record(succeeded)

    async def run_restore_job(
        self,
        job_id: str,
        *,
        authorize: RestoreAuthorizeCallback,
    ) -> dict[str, Any]:
        job_id = _canonical_uuid(job_id, "job_id")
        if not callable(authorize):
            _raise("RESTORE_AUTH_REQUIRED", "restore commit requires a runtime-only authorization callback")
        job = self._get_job(job_id)
        if job.get("kind") != "RESTORE_COMMIT":
            _raise("JOB_KIND_UNSUPPORTED", "run_restore_job accepts RESTORE_COMMIT jobs only")
        if job["phase"] == "SUCCEEDED":
            return self._public_job_record(job)
        if job["phase"] == "RUNNING":
            _raise("JOB_ALREADY_RUNNING", "database job is already running")
        if job["phase"] not in {"QUEUED", "FAILED"}:
            _raise("JOB_STATE_INVALID", "restore job is not executable")
        job = self._transition_job_to_running(job_id)
        try:
            binding, preview = self._restore_commit_execution_binding(job)
            if self._restore_apply is None:
                _raise("JOB_HOOK_UNAVAILABLE", "restore apply hook is not configured")
            raw_result = await _maybe_await(self._restore_apply(dict(binding), dict(preview), authorize))
            result = self._restore_public_result(raw_result)
        except AdapterError as exc:
            code = (
                exc.code
                if isinstance(exc.code, str) and _JOB_ERROR_CODE_RE.fullmatch(exc.code)
                else "DATABASE_JOB_FAILED"
            )
            failed = self._update_job(
                job_id,
                phase="FAILED",
                error={"code": code, "message": "database job failed"},
            )
            return self._public_job_record(failed)
        except Exception:
            failed = self._update_job(
                job_id,
                phase="FAILED",
                error={"code": "DATABASE_JOB_FAILED", "message": "database job failed"},
            )
            return self._public_job_record(failed)
        succeeded = self._update_job(job_id, phase="SUCCEEDED", result=result)
        return self._public_job_record(succeeded)
