from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows security qualification only")

from factlane.windows_security import (
    WindowsSecurityError,
    current_user_sid,
    ensure_owner_private_directory,
    process_identity,
    protect_owner_private_path,
    require_process_identity,
    validate_local_named_pipe_name,
    validate_owner_private_path,
)


def test_windows_current_process_identity_is_bound_to_current_user_sid() -> None:
    sid = current_user_sid()
    identity = process_identity(os.getpid())
    assert sid.startswith("S-")
    assert identity.pid == os.getpid()
    assert identity.sid == sid
    assert identity.creation_time_us > 0
    assert require_process_identity(
        os.getpid(),
        owner_sid=sid,
        creation_time_us=identity.creation_time_us,
    ) == identity


def test_windows_process_identity_rejects_foreign_sid_and_stale_creation_identity() -> None:
    identity = process_identity(os.getpid())
    with pytest.raises(WindowsSecurityError) as sid_error:
        require_process_identity(os.getpid(), owner_sid="S-1-5-18")
    assert sid_error.value.code == "WINDOWS_PROCESS_SID_MISMATCH"

    with pytest.raises(WindowsSecurityError) as start_error:
        require_process_identity(
            os.getpid(),
            owner_sid=identity.sid,
            creation_time_us=identity.creation_time_us + 1,
        )
    assert start_error.value.code == "WINDOWS_PROCESS_IDENTITY_MISMATCH"


def test_windows_owner_private_directory_and_file_use_current_user_dacl(tmp_path) -> None:
    sid = current_user_sid()
    private = tmp_path / "private"
    ensure_owner_private_directory(private, owner_sid=sid)
    validate_owner_private_path(private, owner_sid=sid, directory=True)

    state = private / "state.json"
    state.write_text("{}", encoding="utf-8")
    protect_owner_private_path(state, owner_sid=sid, directory=False)
    validate_owner_private_path(state, owner_sid=sid, directory=False)


def test_windows_named_pipe_namespace_rejects_remote_and_tcp_substitutes() -> None:
    valid = rf"\\.\pipe\factlane-owner-security-{os.getpid()}"
    assert validate_local_named_pipe_name(valid, prefix="factlane-owner-") == valid

    for invalid in (
        rf"\\server\pipe\factlane-owner-security-{os.getpid()}",
        "127.0.0.1:8765",
        rf"\\.\pipe\other-product-{os.getpid()}",
    ):
        with pytest.raises(WindowsSecurityError) as exc_info:
            validate_local_named_pipe_name(invalid, prefix="factlane-owner-")
        assert exc_info.value.code == "WINDOWS_PIPE_NAME_INVALID"
