from pathlib import Path
import inspect
import subprocess

from factlane.adapter import MemoryAdapter


def test_production_package_contains_no_pilot_module() -> None:
    assert not Path("src/factlane/pilot.py").exists()


def test_tracked_generated_egg_info_is_absent() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "src/*.egg-info", "src/*.egg-info/**"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert tracked == ""


def test_update_api_preserves_expected_revision_contract() -> None:
    parameters = inspect.signature(MemoryAdapter.update).parameters
    assert "expected_revision" in parameters


def test_public_product_surface_exists() -> None:
    required = [
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "environment-provenance.json",
        "docs/ARCHITECTURE.md",
        "docs/ENVIRONMENT.md",
        ".github/workflows/ci.yml",
    ]
    missing = [path for path in required if not Path(path).is_file()]
    assert missing == []


def _tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in output.splitlines() if line]


def test_public_tree_excludes_private_operational_surfaces() -> None:
    tracked = _tracked_files()
    forbidden_exact = {
        "AGENTS.md",
        "TASKBOARD.md",
        "docs/GOVERNANCE.md",
        "docs/WORKFLOW_TEMPLATE.md",
        "docs/S6B_4C_04_DISPOSABLE_HOST_ACCEPTANCE_RUNBOOK.md",
        "docs/S6B_4C_SHARED_STORE_CONCURRENCY_SPEC.md",
        "src/factlane/execution_context.py",
        "tests/unit/test_execution_context.py",
        "tests/acceptance/s6b4b_pilot.py",
        "tests/integration/test_disposable_host_concurrency.py",
        "tests/integration/test_process_crash_acceptance.py",
        "tools/s6b4c04_disposable_shared_store.py",
        "tools/s6b4c06_crash_acceptance.py",
    }
    forbidden_prefixes = ("docs/superpowers/", ".factlane-local/")
    forbidden = [
        path
        for path in tracked
        if path in forbidden_exact or path.startswith(forbidden_prefixes)
    ]
    assert forbidden == []


def test_public_tree_tracks_no_runtime_or_private_evidence() -> None:
    tracked = _tracked_files()
    forbidden_suffixes = (".db", ".sqlite", ".sqlite3")
    assert not any(path.endswith(forbidden_suffixes) for path in tracked)
    assert not any(path.startswith("pilot-evidence/") for path in tracked)
    assert not any(".egg-info/" in path for path in tracked)


def test_minimal_ci_workflow_is_tracked() -> None:
    assert ".github/workflows/ci.yml" in _tracked_files() or Path(".github/workflows/ci.yml").is_file()
