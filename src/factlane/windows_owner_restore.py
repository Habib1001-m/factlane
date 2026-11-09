from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable

from .contract import AdapterError
from .owner_database import (
    ArtifactRegistry,
    _open_new_private_file,
    _open_windows_parent_guard,
    _unlink_private_file,
    _validate_private_regular_file,
    _windows_handle_final_path,
    create_consistent_backup,
)


_RETAINED_STAGE_CUSTODY: list[Any] = []


class WindowsRestoreMutationError(AdapterError):
    """Fail-closed native Windows restore mutation failure."""

    def __init__(self, code: str, safe_message: str, *, mutation_may_have_started: bool) -> None:
        super().__init__(code, safe_message)
        self.mutation_may_have_started = mutation_may_have_started


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_receipt(identity: Any) -> dict[str, Any]:
    if identity is None:
        raise AdapterError("WINDOWS_RESTORE_KERNEL_INVALID", "Windows restore lease lost active database identity")
    volume = getattr(identity, "volume_serial_number", None)
    file_id_hex = getattr(identity, "file_id_hex", None)
    final_path = getattr(identity, "final_path", None)
    if isinstance(volume, int) and isinstance(file_id_hex, str) and file_id_hex and isinstance(final_path, str):
        return {"volume_serial_number": volume, "file_id": file_id_hex, "final_path": final_path}
    if isinstance(identity, tuple) and len(identity) == 2 and all(isinstance(item, int) for item in identity):
        return {"device": int(identity[0]), "inode": int(identity[1])}
    raise AdapterError("WINDOWS_RESTORE_KERNEL_INVALID", "Windows restore lease active identity is malformed")


class WindowsOwnerRestoreOperator:
    """Windows full-restore mutation over one in-place exclusive SQLite lease.

    The lease owns the DB-scoped FactLane exclusion sentinel, deny-DELETE main
    guard, and an EXCLUSIVE/DELETE/FULL SQLite connection.  Restore content is
    prepared off-path, then SQLite's backup API installs it transactionally into
    the already-open active connection so the active file identity never changes.
    """

    def __init__(
        self,
        *,
        active_db: str | os.PathLike[str],
        state_dir: str | os.PathLike[str],
        registry: ArtifactRegistry,
        operation_id: str,
        lease_factory: Callable[[str | os.PathLike[str]], Any] | None = None,
    ) -> None:
        self.active_db = Path(active_db).absolute()
        self.state_dir = Path(state_dir).absolute()
        self.registry = registry
        self.operation_id = operation_id
        self._lease_factory = lease_factory
        self._lease: Any | None = None
        self._active_conn: sqlite3.Connection | None = None
        self._stage_path: Path | None = None
        self._stage_handle: Any | None = None
        self._stage_parent_guard: Any | None = None
        self._stage_identity: tuple[int, int] | None = None
        self._closed = False
        self._retained = False
        self._mutation_started = False
        self._runtime_handoff_prepared = False

    def __enter__(self) -> WindowsOwnerRestoreOperator:
        if self._closed or self._lease is not None:
            raise AdapterError("WINDOWS_RESTORE_OPERATOR_INVALID", "Windows restore operator lifecycle is invalid")
        factory = self._lease_factory
        if factory is None:
            try:
                from .windows_maintenance import WindowsInPlaceMaintenanceLease
            except ImportError as exc:  # pragma: no cover - current kernel gate
                raise AdapterError(
                    "WINDOWS_RESTORE_KERNEL_UNAVAILABLE",
                    "native Windows in-place restore kernel is not yet qualified",
                ) from exc
            factory = WindowsInPlaceMaintenanceLease.acquire
        try:
            lease = factory(self.active_db)
        except Exception as exc:
            code = getattr(exc, "code", "WINDOWS_RESTORE_EXCLUSION_UNAVAILABLE")
            message = getattr(exc, "safe_message", "Windows restore exclusion could not be acquired")
            raise AdapterError(str(code), str(message)) from exc
        try:
            connection = getattr(lease, "active_connection", None)
            if connection is None:
                connection = getattr(lease, "connection", None)
            if not isinstance(connection, sqlite3.Connection):
                raise AdapterError(
                    "WINDOWS_RESTORE_KERNEL_INVALID",
                    "native Windows in-place restore lease did not expose its held SQLite connection",
                )
            if not callable(getattr(lease, "revalidate", None)) or not callable(getattr(lease, "retain_fail_closed", None)):
                raise AdapterError(
                    "WINDOWS_RESTORE_KERNEL_INVALID",
                    "native Windows in-place restore lease contract is incomplete",
                )
            self._lease = lease
            self._active_conn = connection
            self._validate_lease_connection()
            self._lease.revalidate()
            _identity_receipt(self._lease_identity())
            return self
        except BaseException:
            close = getattr(lease, "close", None)
            if callable(close):
                close()
            raise

    def capture_pre_restore_backup(self, *, artifact_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        lease = self._require_lease()
        active_conn = self._require_connection()
        lease.revalidate()
        before_identity = _identity_receipt(self._lease_identity())
        capture = self.state_dir / f".restore-{self.operation_id}.capture.sqlite3"
        fd = -1
        parent_guard = None
        expected_identity: tuple[int, int] | None = None
        try:
            fd, parent_guard = _open_new_private_file(
                capture,
                os.O_WRONLY,
                code="RESTORE_STATE_UNAVAILABLE",
                message="Windows pre-restore capture could not be created privately",
            )
            expected_identity = (int(os.fstat(fd).st_dev), int(os.fstat(fd).st_ino))
            os.close(fd)
            fd = -1
            capture_conn = sqlite3.connect(capture, timeout=5.0)
            try:
                active_conn.backup(capture_conn)
                capture_conn.commit()
                integrity = capture_conn.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise AdapterError("BACKUP_VALIDATION_FAILED", "Windows pre-restore capture failed integrity verification")
            finally:
                capture_conn.close()
            lease.revalidate()
            after_identity = _identity_receipt(self._lease_identity())
            if after_identity != before_identity:
                raise AdapterError("WINDOWS_RESTORE_IDENTITY_DRIFT", "active database identity changed during Windows snapshot")
            receipt = create_consistent_backup(capture, self.registry, artifact_id=artifact_id)
            snapshot_receipt = {
                "active_identity": before_identity,
                "capture_size_bytes": capture.stat().st_size,
                "capture_sha256": _sha256_file(capture),
            }
            return receipt, snapshot_receipt
        except Exception as exc:
            if isinstance(exc, AdapterError):
                raise
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_SNAPSHOT_FAILED",
                "Windows pre-restore snapshot failed under exclusion",
                mutation_may_have_started=False,
            ) from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if capture.exists():
                _unlink_private_file(capture, parent_guard=parent_guard, expected_identity=expected_identity)
            if parent_guard is not None:
                parent_guard.close()

    def prepare_activation(self, stage: Path) -> dict[str, Any]:
        lease = self._require_lease()
        lease.revalidate()
        info = stage.stat()
        if not stage.is_file():
            raise AdapterError("RESTORE_STAGE_INVALID", "Windows restore stage is not a regular file")
        self._validate_stage_interlock(stage, required=True)
        self._bind_stage(stage)
        return {
            "operation_id": self.operation_id,
            "stage_path": str(stage),
            "stage_device": int(info.st_dev),
            "stage_inode": int(info.st_ino),
            "stage_size_bytes": int(info.st_size),
            "stage_sha256": _sha256_file(stage),
            "active_identity": _identity_receipt(self._lease_identity()),
        }

    def activate_and_verify(self, intent: dict[str, Any]) -> dict[str, Any]:
        lease = self._require_lease()
        active_conn = self._require_connection()
        backup_started = False
        try:
            stage = self._validated_stage_from_intent(intent)
            lease.revalidate()
            expected_active = intent.get("active_identity")
            if _identity_receipt(self._lease_identity()) != expected_active:
                raise AdapterError("WINDOWS_RESTORE_IDENTITY_DRIFT", "active database identity changed before restore activation")
            stage_conn = sqlite3.connect(f"file:{stage.as_posix()}?mode=ro", uri=True, timeout=5.0)
            try:
                self._validate_connection_interlock(stage_conn, required=True)
                integrity = stage_conn.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise AdapterError("BACKUP_INVALID", "Windows restore stage failed integrity verification")
                backup_started = True
                self._mutation_started = True
                stage_conn.backup(active_conn)
            finally:
                stage_conn.close()
            lease.revalidate()
            if _identity_receipt(self._lease_identity()) != expected_active:
                raise AdapterError("WINDOWS_RESTORE_IDENTITY_DRIFT", "active database identity changed during restore activation")
            proof = self._verify_active_image(intent, interlock_required=True)
            self._cleanup_stage_if_present(intent)
            return proof
        except Exception as exc:
            if isinstance(exc, WindowsRestoreMutationError):
                raise
            if isinstance(exc, AdapterError):
                raise WindowsRestoreMutationError(
                    exc.code,
                    exc.safe_message,
                    mutation_may_have_started=backup_started,
                ) from exc
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_MUTATION_FAILED",
                "native Windows in-place restore mutation failed",
                mutation_may_have_started=backup_started,
            ) from exc

    def recover_activation(self, intent: dict[str, Any]) -> dict[str, Any]:
        active_conn = self._require_connection()
        interlock = self._read_interlock(active_conn)
        if interlock == self.operation_id:
            self._mutation_started = True
            proof = self._verify_active_image(intent, interlock_required=True)
            self._cleanup_stage_if_present(intent)
            return proof
        if interlock is None:
            return self.activate_and_verify(intent)
        raise WindowsRestoreMutationError(
            "WINDOWS_RESTORE_INTERLOCK_CONFLICT",
            "active database carries another recovery interlock",
            mutation_may_have_started=True,
        )

    def classify_activation(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Classify an interrupted SQLite backup without performing another mutation."""

        lease = self._require_lease()
        conn = self._require_connection()
        lease.revalidate()
        if _identity_receipt(self._lease_identity()) != intent.get("active_identity"):
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_IDENTITY_DRIFT",
                "active database identity changed while classifying interrupted restore",
                mutation_may_have_started=True,
            )
        interlock = self._read_interlock(conn)
        if interlock == self.operation_id:
            proof = self._verify_active_image(intent, interlock_required=True)
            self._cleanup_stage_if_present(intent)
            return {"state": "COMMITTED", "proof": proof}
        if interlock is None:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise WindowsRestoreMutationError(
                    "WINDOWS_RESTORE_OUTCOME_UNPROVEN",
                    "interrupted Windows restore left an unprovable active database",
                    mutation_may_have_started=True,
                )
            return {"state": "NOT_COMMITTED", "active_identity": _identity_receipt(self._lease_identity())}
        raise WindowsRestoreMutationError(
            "WINDOWS_RESTORE_OUTCOME_UNPROVEN",
            "interrupted Windows restore outcome could not be classified safely",
            mutation_may_have_started=True,
        )

    def verify_active(self, intent: dict[str, Any], proof: dict[str, Any], *, interlock_required: bool) -> dict[str, Any]:
        current = self._verify_active_image(intent, interlock_required=interlock_required)
        expected_identity = proof.get("active_identity")
        if current.get("active_identity") != expected_identity:
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_IDENTITY_DRIFT",
                "active database no longer matches the verified Windows restore identity",
                mutation_may_have_started=True,
            )
        return current

    def prepare_runtime_handoff(self) -> dict[str, Any]:
        """Return the held database to FactLane's normal WAL runtime posture."""

        lease = self._require_lease()
        conn = self._require_connection()
        lease.revalidate()
        prepare = getattr(lease, "prepare_runtime_activation", None)
        if not callable(prepare):
            raise AdapterError(
                "WINDOWS_RESTORE_KERNEL_INVALID",
                "Windows in-place maintenance lease cannot prepare runtime activation",
            )
        try:
            prepare()
        except Exception as exc:
            raise WindowsRestoreMutationError(
                str(getattr(exc, "code", "WINDOWS_RESTORE_RUNTIME_HANDOFF_FAILED")),
                str(getattr(exc, "safe_message", "restored database could not return to normal WAL runtime mode")),
                mutation_may_have_started=True,
            ) from exc
        connection = getattr(lease, "connection", None)
        if not isinstance(connection, sqlite3.Connection):
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_RUNTIME_HANDOFF_FAILED",
                "Windows maintenance lease lost its runtime SQLite connection",
                mutation_may_have_started=True,
            )
        self._active_conn = connection
        conn = connection
        observed = {
            "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold(),
            "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
            "busy_timeout_ms": int(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
            "locking_mode": str(conn.execute("PRAGMA locking_mode").fetchone()[0]).casefold(),
        }
        if (
            observed["journal_mode"] != "wal"
            or observed["synchronous"] != 1
            or observed["busy_timeout_ms"] < 5000
            or observed["locking_mode"] != "normal"
        ):
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_RUNTIME_HANDOFF_FAILED",
                "restored database runtime SQLite posture could not be verified",
                mutation_may_have_started=True,
            )
        lease.revalidate()
        self._runtime_handoff_prepared = True
        return observed

    def clear_interlock(self) -> None:
        conn = self._require_connection()
        current = self._read_interlock(conn)
        if current != self.operation_id:
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_INTERLOCK_CONFLICT",
                "Windows restore interlock does not match the active operation",
                mutation_may_have_started=True,
            )
        conn.execute("DELETE FROM adapter_meta WHERE key='sensitive_recovery_interlock'")
        conn.commit()
        checkpoint = conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_INTERLOCK_CLEAR_FAILED",
                "Windows restore interlock clear could not be checkpointed durably",
                mutation_may_have_started=True,
            )
        self._require_lease().flush_file_buffers()
        if self._read_interlock(conn) is not None:
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_INTERLOCK_CLEAR_FAILED",
                "Windows restore interlock could not be cleared durably",
                mutation_may_have_started=True,
            )

    def restore_interlock(self) -> None:
        conn = self._require_connection()
        current = self._read_interlock(conn)
        if current not in {None, self.operation_id}:
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_INTERLOCK_CONFLICT",
                "active database carries another recovery interlock",
                mutation_may_have_started=True,
            )
        conn.execute(
            "INSERT OR REPLACE INTO adapter_meta(key,value) VALUES ('sensitive_recovery_interlock',?)",
            (self.operation_id,),
        )
        conn.commit()
        checkpoint = conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_INTERLOCK_RESTORE_FAILED",
                "Windows restore interlock restore could not be checkpointed durably",
                mutation_may_have_started=True,
            )
        self._require_lease().flush_file_buffers()
        if self._read_interlock(conn) != self.operation_id:
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_INTERLOCK_RESTORE_FAILED",
                "Windows restore interlock could not be restored durably",
                mutation_may_have_started=True,
            )

    def retain_fail_closed(self) -> None:
        lease = self._require_lease()
        lease.retain_fail_closed()
        if not self._retained:
            self._retained = True
            for retained in (self._stage_handle, self._stage_parent_guard):
                if retained is not None:
                    _RETAINED_STAGE_CUSTODY.append(retained)
            self._stage_handle = None
            self._stage_parent_guard = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._retained:
            self._close_stage_custody()
        self._active_conn = None
        lease, self._lease = self._lease, None
        if lease is not None:
            if not self._mutation_started and not self._runtime_handoff_prepared:
                release_unmutated = getattr(lease, "release_unmutated", None)
                if callable(release_unmutated):
                    release_unmutated()
                    return
            close = getattr(lease, "close", None)
            if callable(close):
                close()

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if _exc_type is not None:
            if self._mutation_started and not self._runtime_handoff_prepared and not self._retained:
                try:
                    self.retain_fail_closed()
                except Exception:
                    pass
                return
            try:
                self.close()
            except Exception:
                lease = self._lease
                retain = getattr(lease, "retain_fail_closed", None) if lease is not None else None
                if callable(retain):
                    try:
                        retain()
                    except Exception:
                        pass
            return
        self.close()

    def _validate_lease_connection(self) -> None:
        conn = self._require_connection()
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
        synchronous = conn.execute("PRAGMA synchronous").fetchone()
        locking_mode = conn.execute("PRAGMA locking_mode").fetchone()
        if (
            journal_mode is None
            or str(journal_mode[0]).lower() != "delete"
            or synchronous is None
            or int(synchronous[0]) != 2
            or locking_mode is None
            or str(locking_mode[0]).lower() != "exclusive"
        ):
            raise AdapterError(
                "WINDOWS_RESTORE_KERNEL_INVALID",
                "Windows in-place restore lease did not hold DELETE/FULL/EXCLUSIVE SQLite mode",
            )

    def _validated_stage_from_intent(self, intent: dict[str, Any]) -> Path:
        if not isinstance(intent, dict) or intent.get("operation_id") != self.operation_id:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore activation intent is malformed")
        raw = intent.get("stage_path")
        if not isinstance(raw, str):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore stage identity is missing")
        stage = Path(raw)
        if not stage.is_absolute() or stage.parent != self.active_db.parent:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore stage path is outside the active database directory")
        if self._stage_handle is None or self._stage_path != stage:
            self._bind_stage(stage)
        self._require_stage_binding(stage)
        info = stage.stat()
        if (
            int(info.st_dev) != intent.get("stage_device")
            or int(info.st_ino) != intent.get("stage_inode")
            or int(info.st_size) != intent.get("stage_size_bytes")
            or _sha256_file(stage) != intent.get("stage_sha256")
        ):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore stage identity changed before activation")
        self._validate_stage_interlock(stage, required=True)
        return stage

    def _bind_stage(self, stage: Path) -> None:
        if self._stage_handle is not None or self._stage_parent_guard is not None:
            if self._stage_path == stage:
                self._require_stage_binding(stage)
                return
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore operator already holds another stage")
        if os.name != "nt":
            info = stage.stat()
            self._stage_path = stage
            self._stage_identity = (int(info.st_dev), int(info.st_ino))
            return
        try:
            import win32con
            import win32file

            guard = _open_windows_parent_guard(
                stage,
                code="RESTORE_STATE_UNAVAILABLE",
                message="Windows restore stage parent cannot be retained safely",
            )
            handle = win32file.CreateFile(
                str(stage),
                int(win32con.GENERIC_READ),
                int(win32con.FILE_SHARE_READ) | int(win32con.FILE_SHARE_WRITE),
                None,
                int(win32con.OPEN_EXISTING),
                int(win32con.FILE_ATTRIBUTE_NORMAL)
                | int(getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000)),
                None,
            )
            try:
                guard.require_current()
                final_path = Path(
                    _windows_handle_final_path(
                        int(handle),
                        code="RESTORE_STATE_UNAVAILABLE",
                        message="Windows restore stage handle identity is unavailable",
                    )
                )
                if os.path.normcase(os.path.abspath(str(final_path))) != os.path.normcase(os.path.abspath(str(stage))):
                    raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore stage handle resolved to another file")
                info = _validate_private_regular_file(
                    stage,
                    code="RESTORE_STATE_UNAVAILABLE",
                    message="Windows restore stage is not owner-controlled",
                )
                self._stage_path = stage
                self._stage_identity = (int(info.st_dev), int(info.st_ino))
                self._stage_parent_guard = guard
                self._stage_handle = handle
            except BaseException:
                handle.Close()
                guard.close()
                raise
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore stage could not be retained safely") from exc

    def _require_stage_binding(self, stage: Path) -> None:
        if self._stage_path != stage or self._stage_identity is None:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore stage binding is unavailable")
        if self._stage_parent_guard is not None:
            self._stage_parent_guard.require_current()
        if self._stage_handle is not None:
            final_path = Path(
                _windows_handle_final_path(
                    int(self._stage_handle),
                    code="RESTORE_STATE_UNAVAILABLE",
                    message="Windows restore stage handle identity is unavailable",
                )
            )
            if os.path.normcase(os.path.abspath(str(final_path))) != os.path.normcase(os.path.abspath(str(stage))):
                raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore stage handle identity drifted")
        info = stage.stat()
        if (int(info.st_dev), int(info.st_ino)) != self._stage_identity:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore stage path identity drifted")

    def _cleanup_stage_if_present(self, intent: dict[str, Any]) -> None:
        raw = intent.get("stage_path")
        if not isinstance(raw, str):
            return
        stage = Path(raw)
        if not stage.exists():
            self._close_stage_custody()
            return
        self._require_stage_binding(stage)
        expected = self._stage_identity
        handle, self._stage_handle = self._stage_handle, None
        if handle is not None:
            try:
                handle.Close()
            except Exception:
                pass
        if not _unlink_private_file(stage, parent_guard=self._stage_parent_guard, expected_identity=expected):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore stage cleanup could not be proven safe")
        self._close_stage_custody()

    def _close_stage_custody(self) -> None:
        handle, self._stage_handle = self._stage_handle, None
        if handle is not None:
            try:
                handle.Close()
            except Exception:
                pass
        guard, self._stage_parent_guard = self._stage_parent_guard, None
        if guard is not None:
            guard.close()
        self._stage_path = None
        self._stage_identity = None

    def _verify_active_image(self, intent: dict[str, Any], *, interlock_required: bool) -> dict[str, Any]:
        lease = self._require_lease()
        conn = self._require_connection()
        lease.revalidate()
        active_identity = _identity_receipt(self._lease_identity())
        if active_identity != intent.get("active_identity"):
            raise AdapterError("WINDOWS_RESTORE_IDENTITY_DRIFT", "active database identity changed during restore")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise AdapterError("BACKUP_INVALID", "restored Windows database failed integrity verification")
        self._validate_connection_interlock(conn, required=interlock_required)
        return {
            "active_identity": active_identity,
            "active_size_bytes": self.active_db.stat().st_size,
            "interlock_present": interlock_required,
            "stage_sha256": intent.get("stage_sha256"),
        }

    def _validate_stage_interlock(self, stage: Path, *, required: bool) -> None:
        conn = sqlite3.connect(f"file:{stage.as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            self._validate_connection_interlock(conn, required=required)
        finally:
            conn.close()

    def _validate_connection_interlock(self, conn: sqlite3.Connection, *, required: bool) -> None:
        value = self._read_interlock(conn)
        if required and value != self.operation_id:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore image lost its operation interlock")
        if not required and value is not None:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore image still carries a recovery interlock")

    @staticmethod
    def _read_interlock(conn: sqlite3.Connection) -> str | None:
        row = conn.execute(
            "SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'"
        ).fetchone()
        if row is None:
            return None
        value = row[0]
        if not isinstance(value, str):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "Windows restore interlock is malformed")
        return value

    def _lease_identity(self) -> Any:
        lease = self._require_lease()
        identity = getattr(lease, "main_identity", None)
        if identity is None:
            identity = getattr(lease, "identity", None)
        return identity

    def _require_lease(self) -> Any:
        if self._closed or self._lease is None:
            raise AdapterError("WINDOWS_RESTORE_OPERATOR_INVALID", "Windows restore operator is not active")
        return self._lease

    def _require_connection(self) -> sqlite3.Connection:
        if self._closed or self._active_conn is None:
            raise AdapterError("WINDOWS_RESTORE_OPERATOR_INVALID", "Windows restore SQLite lease is not active")
        return self._active_conn


__all__ = ["WindowsOwnerRestoreOperator", "WindowsRestoreMutationError"]
