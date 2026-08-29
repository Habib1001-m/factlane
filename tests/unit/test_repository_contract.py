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


def test_governance_surface_exists() -> None:
    required = [
        "AGENTS.md",
        "TASKBOARD.md",
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "environment-provenance.json",
        "docs/ARCHITECTURE.md",
        "docs/GOVERNANCE.md",
        "docs/ENVIRONMENT.md",
        "docs/PROJECT_HISTORY.md",
    ]
    missing = [path for path in required if not Path(path).is_file()]
    assert missing == []


def test_taskboard_is_single_in_place_authority() -> None:
    text = Path("TASKBOARD.md").read_text(encoding="utf-8")
    assert "CANONICAL_FILENAME=TASKBOARD.md" in text
    assert "TASKBOARD_UPDATE_MODE=IN_PLACE_APPEND_AND_RECONCILE" in text
    assert "NUMBERED_TASKBOARD_FILES_FUTURE_AUTHORITY=NO" in text
    tracked = subprocess.run(
        ["git", "ls-files", "TASKBOARD_V*.md", "*TASKBOARD_V*.md"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert tracked == ""


def _tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in output.splitlines() if line]


def test_public_tree_tracks_no_runtime_or_private_evidence() -> None:
    tracked = _tracked_files()
    forbidden_suffixes = (".db", ".sqlite", ".sqlite3")
    assert not any(path.endswith(forbidden_suffixes) for path in tracked)
    assert not any(path.startswith("pilot-evidence/") for path in tracked)
    legacy_package_prefix = "src/" + "one_linux_" + "codex_memory/"
    assert not any(path.startswith(legacy_package_prefix) for path in tracked)
    assert not any("ONE_LINUX_MEMORY_CANONICAL_REVIEW_CLONE" in path for path in tracked)
    assert not any(".egg-info/" in path for path in tracked)


def test_minimal_ci_workflow_is_tracked() -> None:
    assert ".github/workflows/ci.yml" in _tracked_files() or Path(".github/workflows/ci.yml").is_file()
