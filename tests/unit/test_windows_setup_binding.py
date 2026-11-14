from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sqlite3

import pytest

from factlane.owner_http import OwnerSetupError
from factlane.webui_app import (
    _WindowsSetupBindingService,
    _configured_embedding_profile,
    _validate_existing_windows_setup_database,
)
from factlane.webui_windows import (
    WindowsManagedRestartPlan,
    WindowsManagedConfig,
    WindowsWebUIError,
    canonical_windows_database_path,
    windows_managed_python_argv,
)
from factlane.storage import SQLiteVecEngine


SID = "S-1-5-21-100-200-300-1001"
CONFIG_PATH = r"C:\Users\Owner\AppData\Local\FactLane\config\webui.json"


def _baseline() -> WindowsManagedConfig:
    return WindowsManagedConfig(
        installation_id="a2native123",
        origin="https://factlane-a2native123.test:8765",
        native_control_pipe=r"\\.\pipe\factlane-owner-deadbeefdeadbeef",
        runtime_dir=r"C:\Users\Owner\AppData\Local\FactLane\runtime",
        state_dir=r"C:\Users\Owner\AppData\Local\FactLane\state",
        browser_profile_dir=r"C:\Users\Owner\AppData\Local\FactLane\browser",
    )


class _RestartPlan:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.handoffs = 0

    def handoff(self) -> None:
        self.handoffs += 1
        if self.fail:
            raise WindowsWebUIError("WINDOWS_RESTART_HANDOFF_FAILED", "managed restart helper could not be started")


def _service(*, db_validator=None, db_inspector=None, restart_plan: _RestartPlan | None = None):
    baseline = _baseline()
    store = {"config": baseline}
    writes: list[WindowsManagedConfig] = []
    plan = restart_plan or _RestartPlan()

    def loader(path: str, *, owner_sid: str | None = None):
        assert path == CONFIG_PATH
        assert owner_sid == SID
        return store["config"]

    def snapshot(path: str, *, owner_sid: str | None = None):
        assert path == CONFIG_PATH
        assert owner_sid == SID
        content = json.dumps(
            store["config"].__dict__,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        return {
            "exists": True,
            "identity": [1, 1],
            "content": base64.b64encode(content.encode()).decode(),
            "sddl": "owner",
        }

    def writer(
        path: str,
        content: str,
        *,
        owner_sid: str | None = None,
        expected_state=None,
    ) -> None:
        assert path == CONFIG_PATH
        assert owner_sid == SID
        assert expected_state == snapshot(path, owner_sid=owner_sid)
        config = WindowsManagedConfig(**json.loads(content))
        writes.append(config)
        store["config"] = config

    service = _WindowsSetupBindingService(
        config_path=CONFIG_PATH,
        baseline_config=baseline,
        owner_sid=SID,
        supported_profiles={"nomic-768", "minilm-384"},
        config_loader=loader,
        config_writer=writer,
        config_snapshot=snapshot,
        mutation_guard=contextlib.nullcontext,
        database_validator=db_validator or (lambda path, *, owner_sid: path),
        database_inspector=db_inspector or (lambda _path, _profile: None),
        restart_preparer=lambda: plan,
    )
    return service, store, writes, plan


def test_setup_binding_publishes_only_memory_fields_then_hands_off_restart() -> None:
    service, store, writes, plan = _service()

    status = service.status()
    assert status == {
        "status": "SETUP_REQUIRED",
        "profiles": ["minilm-384", "nomic-768"],
        "suggested_host_id": "owner-a2native123",
        "suggested_db": r"C:\Users\Owner\AppData\Local\FactLane\state\memory.db",
    }

    result = service.bind(
        db=r"C:\Users\Owner\FactLane\memory.db",
        profile="nomic-768",
        host_id="owner-a2native123",
        request_id="setup-request-1",
    )
    assert result == {
        "status": "CONFIG_PUBLISHED",
        "restart": "REQUESTED",
        "request_id": "setup-request-1",
    }
    assert plan.handoffs == 0
    assert len(writes) == 1
    published = store["config"]
    baseline = _baseline()
    assert published.db == r"C:\Users\Owner\FactLane\memory.db"
    assert published.profile == "nomic-768"
    assert published.host_id == "owner-a2native123"
    for field in (
        "installation_id",
        "origin",
        "native_control_pipe",
        "runtime_dir",
        "state_dir",
        "browser_profile_dir",
        "version",
    ):
        assert getattr(published, field) == getattr(baseline, field)

    assert service.handoff_restart("setup-request-1") is True
    assert plan.handoffs == 1
    assert service.status()["status"] == "CONFIG_PUBLISHED_RESTART_REQUESTED"


@pytest.mark.parametrize(
    ("db", "profile", "host_id", "expected_code"),
    [
        (r"C:\Users\Owner\memory.db", "unsupported", "owner-a2native123", "SETUP_PROFILE_UNSUPPORTED"),
        (r"C:\Users\Owner\memory.db", "nomic-768", "not safe!", "SETUP_HOST_ID_INVALID"),
    ],
)
def test_setup_binding_rejects_invalid_values_before_config_write(
    db: str,
    profile: str,
    host_id: str,
    expected_code: str,
) -> None:
    service, store, writes, plan = _service()

    with pytest.raises(OwnerSetupError) as raised:
        service.bind(db=db, profile=profile, host_id=host_id, request_id="setup-request-1")

    assert raised.value.code == expected_code
    assert store["config"] == _baseline()
    assert writes == []
    assert plan.handoffs == 0


@pytest.mark.parametrize("security_code", ["WINDOWS_PATH_REPARSE_DENIED", "WINDOWS_DACL_UNSAFE"])
def test_setup_binding_rejects_database_custody_failure_before_config_write(security_code: str) -> None:
    def reject_database(_path: str, *, owner_sid: str) -> str:
        assert owner_sid == SID
        raise WindowsWebUIError(security_code, "database custody is unsafe")

    service, store, writes, plan = _service(db_validator=reject_database)

    with pytest.raises(OwnerSetupError) as raised:
        service.bind(
            db=r"C:\Users\Owner\FactLane\memory.db",
            profile="nomic-768",
            host_id="owner-a2native123",
            request_id="setup-request-1",
        )

    assert raised.value.code == security_code
    assert store["config"] == _baseline()
    assert writes == []
    assert plan.handoffs == 0


@pytest.mark.parametrize(
    "code",
    [
        "SETUP_DATABASE_MIGRATION_REQUIRED",
        "SETUP_DATABASE_INVALID",
        "SETUP_DATABASE_PROFILE_MISMATCH",
    ],
)
def test_setup_binding_rejects_existing_database_preflight_before_config_write(code: str) -> None:
    def reject_existing(_path: str, _profile: str) -> None:
        raise OwnerSetupError(code, "existing database is not safe to bind", status=409)

    service, store, writes, plan = _service(db_inspector=reject_existing)

    with pytest.raises(OwnerSetupError) as raised:
        service.bind(
            db=r"C:\Users\Owner\FactLane\memory.db",
            profile="nomic-768",
            host_id="owner-a2native123",
            request_id="setup-request-1",
        )

    assert raised.value.code == code
    assert store["config"] == _baseline()
    assert writes == []
    assert plan.handoffs == 0


def test_setup_binding_preflights_restart_before_config_publication() -> None:
    service, store, writes, _plan = _service()
    service._restart_preparer = lambda: (_ for _ in ()).throw(
        WindowsWebUIError("WINDOWS_RESTART_UNAVAILABLE", "managed restart cannot be proven")
    )

    with pytest.raises(OwnerSetupError) as raised:
        service.bind(
            db=r"C:\Users\Owner\FactLane\memory.db",
            profile="nomic-768",
            host_id="owner-a2native123",
            request_id="setup-request-1",
        )

    assert raised.value.code == "WINDOWS_RESTART_UNAVAILABLE"
    assert store["config"] == _baseline()
    assert writes == []


def test_setup_binding_keeps_published_config_truthful_if_post_response_restart_handoff_fails() -> None:
    failing_plan = _RestartPlan(fail=True)
    service, store, writes, _plan = _service(restart_plan=failing_plan)
    service.bind(
        db=r"C:\Users\Owner\FactLane\memory.db",
        profile="nomic-768",
        host_id="owner-a2native123",
        request_id="setup-request-1",
    )

    assert service.handoff_restart("setup-request-1") is False
    assert failing_plan.handoffs == 1
    assert store["config"].db == r"C:\Users\Owner\FactLane\memory.db"
    assert len(writes) == 1
    status = service.status()
    assert status["status"] == "CONFIG_PUBLISHED_RESTART_REQUIRED"
    assert status["restart_error"] == "WINDOWS_RESTART_HANDOFF_FAILED"


@pytest.mark.parametrize(
    "path",
    [
        r"C:/Users/Owner/FactLane/memory.db",
        r"C:\Users\Owner\FactLane\..\memory.db",
        r"\\wsl.localhost\Ubuntu\home\owner\memory.db",
        r"\\server\share\memory.db",
        "/home/owner/memory.db",
        r"C:\Users\Owner\memory.db:shadow",
        r"C:\Users\Owner\NUL.db",
        "C:\\Users\\Owner\\memory.db. ",
    ],
)
def test_canonical_windows_database_path_rejects_noncanonical_wsl_linux_and_unc_paths(path: str) -> None:
    with pytest.raises(WindowsWebUIError):
        canonical_windows_database_path(path)


def test_canonical_windows_database_path_accepts_exact_native_drive_path() -> None:
    path = r"C:\Users\Owner\FactLane\memory.db"
    assert canonical_windows_database_path(path) == path


def test_existing_setup_database_v2_requires_migration_and_arbitrary_sqlite_is_invalid(tmp_path) -> None:
    v2 = tmp_path / "v2.db"
    conn = sqlite3.connect(v2)
    conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO adapter_meta VALUES ('contract_version','2')")
    conn.commit()
    conn.close()

    with pytest.raises(OwnerSetupError) as raised:
        _validate_existing_windows_setup_database(str(v2), "nomic-768")
    assert raised.value.code == "SETUP_DATABASE_MIGRATION_REQUIRED"

    arbitrary = tmp_path / "arbitrary.db"
    conn = sqlite3.connect(arbitrary)
    conn.execute("CREATE TABLE unrelated (value TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(OwnerSetupError) as raised:
        _validate_existing_windows_setup_database(str(arbitrary), "nomic-768")
    assert raised.value.code == "SETUP_DATABASE_INVALID"


def test_existing_setup_database_v3_requires_matching_stored_embedding_profile(tmp_path) -> None:
    database = tmp_path / "v3.db"
    profile = _configured_embedding_profile("nomic-768")
    engine = SQLiteVecEngine(str(database), profile)
    asyncio.run(engine.open())
    asyncio.run(engine.close())

    metadata = {
        "metadata_revision": 2,
        "store_key": "nomic-768",
        "semantic_identity": profile.semantic_identity(),
        "compatibility_proof": {},
        "runtime_provenance": {},
        "runtime_fingerprint": "f" * 64,
    }
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT OR REPLACE INTO adapter_meta(key,value) VALUES ('embedding_profile',?)",
        (json.dumps(metadata, separators=(",", ":"), sort_keys=True),),
    )
    conn.commit()
    conn.close()

    _validate_existing_windows_setup_database(str(database), "nomic-768")

    with pytest.raises(OwnerSetupError) as raised:
        _validate_existing_windows_setup_database(str(database), "minilm-384")
    assert raised.value.code == "SETUP_DATABASE_PROFILE_MISMATCH"


def test_existing_setup_database_allows_only_missing_fresh_path_without_contract(tmp_path) -> None:
    _validate_existing_windows_setup_database(str(tmp_path / "new-memory.db"), "nomic-768")

    empty = tmp_path / "empty.db"
    empty.touch()
    with pytest.raises(OwnerSetupError) as raised:
        _validate_existing_windows_setup_database(str(empty), "nomic-768")
    assert raised.value.code == "SETUP_DATABASE_INVALID"


def test_windows_restart_handoff_uses_isolated_module_cli_without_shell_or_binding_values() -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class Process:
        pid = 4321

        @staticmethod
        def poll():
            return None

    def spawn(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return Process()

    plan = WindowsManagedRestartPlan(
        executable=r"C:\Program Files\Python311\python.exe",
        environment_executable=r"C:\Program Files\FactLane\.venv\Scripts\python.exe",
        creationflags=123,
        process_factory=spawn,
    )
    plan.handoff()

    assert calls[0][0] == (
        r"C:\Program Files\Python311\python.exe",
        *windows_managed_python_argv(
            r"C:\Program Files\FactLane\.venv\Scripts\python.exe",
            "restart",
        ),
    )
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["creationflags"] == 123
    serialized = repr(calls[0])
    assert "memory.db" not in serialized
    assert "nomic-768" not in serialized
