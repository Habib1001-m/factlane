from __future__ import annotations

from pathlib import Path
import subprocess
import tarfile
import tomllib
from zipfile import ZipFile


SKILL_ROOT = Path("skills/using-factlane")
SKILL_ENTRYPOINT = SKILL_ROOT / "SKILL.md"
HOST_BOOTSTRAP = SKILL_ROOT / "references/host-bootstrap.md"
EXPECTED_REFERENCE_SET = {
    "SKILL.md": SKILL_ENTRYPOINT,
    "references/host-bootstrap.md": HOST_BOOTSTRAP,
}


def _source_bytes() -> dict[str, bytes]:
    return {relative: source.read_bytes() for relative, source in EXPECTED_REFERENCE_SET.items()}


def test_skill_reference_set_is_tracked_and_packaged_by_configuration() -> None:
    assert all(path.is_file() for path in EXPECTED_REFERENCE_SET.values())
    actual_source_files = {
        path.relative_to(SKILL_ROOT).as_posix()
        for path in SKILL_ROOT.rglob("*")
        if path.is_file()
    }
    assert actual_source_files == set(EXPECTED_REFERENCE_SET)

    tracked = subprocess.run(
        ["git", "ls-files", "skills/using-factlane"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert set(tracked) == {
        (SKILL_ROOT / relative).as_posix() for relative in EXPECTED_REFERENCE_SET
    }

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    data_files = project["tool"]["setuptools"]["data-files"]
    assert data_files["share/factlane/skills/using-factlane"] == [
        "skills/using-factlane/SKILL.md"
    ]
    assert data_files["share/factlane/skills/using-factlane/references"] == [
        "skills/using-factlane/references/host-bootstrap.md"
    ]


def test_skill_declares_consent_and_bootstrap_authority_boundaries() -> None:
    skill = SKILL_ENTRYPOINT.read_text(encoding="utf-8")
    normalized = " ".join(skill.split()).casefold()
    for marker in (
        "explicit content consent",
        "agent proposes",
        "consent never elevates launcher/write authority",
        "autonomous post-turn",
        "references/host-bootstrap.md",
        "read-only",
    ):
        assert marker.casefold() in normalized

    bootstrap = HOST_BOOTSTRAP.read_text(encoding="utf-8")
    for marker in (
        "runtime installed",
        "mcp configured",
        "skill present",
        "skill registered",
        "skill discoverable",
        "skill loaded",
        "exactly five",
        "memory_search",
        "memory_get",
        "memory_store",
        "memory_update",
        "memory_status",
        "loaded: unproven",
        "environment mutations",
        "smallest required closure",
    ):
        assert marker.casefold() in bootstrap.casefold()

    assert "host-adapter sdk" in bootstrap.casefold()
    assert "does not automatically register" in bootstrap.casefold()


def test_readme_and_quickstart_remain_read_only_first() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    quickstart = Path("docs/QUICKSTART.md").read_text(encoding="utf-8")
    architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    tools = Path("docs/TOOLS.md").read_text(encoding="utf-8")

    readme_launch = readme.split("### Launch for a local stdio MCP host", 1)[1].split("## Five focused MCP tools", 1)[0]
    assert "--write-profile delegated-candidate" not in readme_launch
    assert "read-only" in readme_launch
    assert "explicit content" in readme
    assert "does not grant or elevate launcher authority" in readme

    first_launch = quickstart.split("For the first connection", 1)[1].split("### Codex", 1)[0]
    assert "--write-profile delegated-candidate" not in first_launch
    assert "read-only" in first_launch
    assert "references/host-bootstrap.md" in quickstart
    assert "v0.1.3` release pinned in step 1 ships `skill.md` only" in quickstart.casefold()
    assert "does **not** include `references/host-bootstrap.md`" in quickstart.casefold()
    assert "current unreleased development source" in quickstart.casefold()
    assert "content consent has two supported entry paths" in quickstart.casefold()
    assert "runtime grant is separate from content consent" in architecture.casefold()
    assert "content consent does not change the launcher profile" in architecture.casefold()
    assert "runtime permission is not content consent" in tools.casefold()


def test_wheel_and_sdist_preserve_complete_skill_reference_set(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    expected = _source_bytes()

    with ZipFile(wheel) as archive:
        names = archive.namelist()
        wheel_bytes: dict[str, bytes] = {}
        for relative in expected:
            suffix = f"/share/factlane/skills/using-factlane/{relative}"
            matches = [name for name in names if name.endswith(suffix)]
            assert len(matches) == 1, (relative, matches)
            wheel_bytes[relative] = archive.read(matches[0])
        assert wheel_bytes == expected

    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        sdist_bytes: dict[str, bytes] = {}
        for relative in expected:
            suffix = f"/skills/using-factlane/{relative}"
            matches = [name for name in names if name.endswith(suffix)]
            assert len(matches) == 1, (relative, matches)
            member = archive.extractfile(matches[0])
            assert member is not None
            sdist_bytes[relative] = member.read()
        assert sdist_bytes == expected
