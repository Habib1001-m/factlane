from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows maintenance coordinator qualification only")

import factlane.maintenance_coordinator as maintenance_coordinator_module
from factlane.maintenance_coordinator import MaintenanceBarrierStore, MaintenanceCoordinatorError
from factlane.windows_security import current_user_sid, validate_owner_private_path


def _create_junction(junction: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")


def test_windows_barrier_state_is_owner_private_from_file_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "memory.db"
    db_path.touch()
    sid = current_user_sid()
    observed_creation_validation: list[str] = []
    original_validate = maintenance_coordinator_module.validate_owner_only_kernel_object

    def validate_at_creation(handle, owner_sid: str) -> None:
        original_validate(handle, owner_sid)
        observed_creation_validation.append(owner_sid)

    monkeypatch.setattr(
        maintenance_coordinator_module,
        "validate_owner_only_kernel_object",
        validate_at_creation,
    )

    store = MaintenanceBarrierStore(tmp_path / "barrier", str(db_path))
    snapshot = store.initialize(generation=7)
    assert snapshot.state == "RUNNING"
    assert observed_creation_validation == [sid]
    validate_owner_private_path(store.state_root, owner_sid=sid, directory=True)
    validate_owner_private_path(store.state_path, owner_sid=sid, directory=False)
    validate_owner_private_path(store.lock_path, owner_sid=sid, directory=False)


def test_windows_barrier_rejects_ancestor_junction_before_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    db_path.touch()
    real = tmp_path / "real"
    real.mkdir()
    junction = tmp_path / "junction"
    _create_junction(junction, real)
    try:
        with pytest.raises(MaintenanceCoordinatorError) as caught:
            MaintenanceBarrierStore(junction / "barrier", str(db_path))
        assert caught.value.code == "BARRIER_UNAVAILABLE"
        assert list(real.iterdir()) == []
    finally:
        os.rmdir(junction)


def test_windows_barrier_guard_is_nonblocking_and_process_crash_releases_handle(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    db_path.touch()
    root = tmp_path / "barrier"
    first = MaintenanceBarrierStore(root, str(db_path))
    second = MaintenanceBarrierStore(root, str(db_path))
    first.initialize(generation=3)

    with first.guard():
        with pytest.raises(MaintenanceCoordinatorError) as busy:
            second.read()
        assert busy.value.code == "BARRIER_BUSY"

    child_code = """
import sys
import time
from factlane.maintenance_coordinator import MaintenanceBarrierStore

store = MaintenanceBarrierStore(sys.argv[1], sys.argv[2])
with store.guard():
    print("READY", flush=True)
    time.sleep(60)
"""
    repo_root = Path(__file__).parents[2]
    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH")
    source_root = str(repo_root / "src")
    env["PYTHONPATH"] = source_root if not current_pythonpath else source_root + os.pathsep + current_pythonpath
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(root), str(db_path)],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        with pytest.raises(MaintenanceCoordinatorError) as busy:
            second.read()
        assert busy.value.code == "BARRIER_BUSY"

        process.kill()
        process.wait(timeout=5)
        assert second.read().generation == 3
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
