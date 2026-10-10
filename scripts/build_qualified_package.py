#!/usr/bin/env python3
"""Build qualification artifacts from the exact committed Git tree.

This is a prerelease/package-integrity gate, not a release action. It does not
assign a version, create a tag, publish assets, or grant deployment authority.
The build runs from a disposable ``git archive`` export so ignored/stale build
state in a reused worktree cannot enter the wheel or source distribution.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message: str) -> None:
    raise SystemExit(f"HOLD: {message}")


def _safe_extract(archive: tarfile.TarFile, target: Path) -> None:
    root = target.resolve()
    for member in archive.getmembers():
        destination = (target / member.name).resolve()
        try:
            destination.relative_to(root)
        except ValueError:
            fail(f"git archive contains unsafe path: {member.name}")
    archive.extractall(target)


def _tracked_package_files(repo: Path, head: str) -> list[str]:
    output = git(
        repo,
        "ls-tree",
        "-r",
        "--name-only",
        head,
        "--",
        "src/factlane",
        "skills/using-factlane",
    )
    return [line for line in output.splitlines() if line]


def _expected_wheel_path(source_path: str, package_name: str, version: str) -> str:
    if source_path.startswith("src/factlane/"):
        return "factlane/" + source_path.removeprefix("src/factlane/")
    if source_path == "skills/using-factlane/SKILL.md":
        relative = "SKILL.md"
    elif source_path.startswith("skills/using-factlane/references/"):
        relative = source_path.removeprefix("skills/using-factlane/")
    else:
        fail(f"unexpected tracked package path: {source_path}")
    dist = package_name.replace("-", "_")
    return f"{dist}-{version}.data/data/share/factlane/skills/using-factlane/{relative}"


def _verify_payload_parity(
    repo: Path,
    head: str,
    wheel: Path,
    sdist: Path,
    *,
    package_name: str,
    version: str,
) -> list[dict[str, str]]:
    tracked = _tracked_package_files(repo, head)
    if not tracked:
        fail("no tracked package payload files were found")

    source_bytes = {
        path: subprocess.check_output(["git", "-C", str(repo), "show", f"{head}:{path}"])
        for path in tracked
    }
    parity: list[dict[str, str]] = []
    sdist_root = f"{package_name}-{version}"

    with zipfile.ZipFile(wheel) as wheel_zip, tarfile.open(sdist, "r:gz") as sdist_tar:
        wheel_names = set(wheel_zip.namelist())
        sdist_names = {member.name for member in sdist_tar.getmembers() if member.isfile()}

        expected_factlane = {
            _expected_wheel_path(path, package_name, version)
            for path in tracked
            if path.startswith("src/factlane/")
        }
        actual_factlane = {
            name for name in wheel_names if name.startswith("factlane/") and not name.endswith("/")
        }
        unexpected = sorted(actual_factlane - expected_factlane)
        missing = sorted(expected_factlane - actual_factlane)
        if unexpected:
            fail("wheel contains package files not present in the committed source tree: " + ", ".join(unexpected))
        if missing:
            fail("wheel is missing committed package files: " + ", ".join(missing))

        forbidden_fragments = (
            ".factlane-control",
            "/tests/",
            "/site/",
            "/.github/",
            "/.factlane-private/",
        )
        for artifact_name, names in (("wheel", wheel_names), ("sdist", sdist_names)):
            leaked = sorted(
                name for name in names if any(fragment in f"/{name}" for fragment in forbidden_fragments)
            )
            if leaked:
                fail(f"{artifact_name} contains private/dev-only material: {', '.join(leaked)}")

        for source_path in tracked:
            wheel_path = _expected_wheel_path(source_path, package_name, version)
            sdist_path = f"{sdist_root}/{source_path}"
            if wheel_path not in wheel_names:
                fail(f"wheel is missing tracked payload: {wheel_path}")
            if sdist_path not in sdist_names:
                fail(f"sdist is missing tracked payload: {sdist_path}")
            wheel_bytes = wheel_zip.read(wheel_path)
            extracted = sdist_tar.extractfile(sdist_path)
            if extracted is None:
                fail(f"could not read sdist payload: {sdist_path}")
            sdist_bytes = extracted.read()
            expected = source_bytes[source_path]
            if wheel_bytes != expected or sdist_bytes != expected:
                fail(f"source/wheel/sdist payload drift: {source_path}")
            parity.append({"path": source_path, "sha256": sha256_bytes(expected)})

    return parity


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--receipt")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    if git(repo, "status", "--porcelain"):
        fail("qualified package build requires a fully clean tracked/untracked worktree")

    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    package_name = pyproject["project"]["name"]
    version = pyproject["project"]["version"]

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.iterdir():
        if existing.is_file() and (existing.suffix == ".whl" or existing.name.endswith(".tar.gz")):
            existing.unlink()

    with tempfile.TemporaryDirectory(prefix="factlane-qualified-package-") as tmp:
        export = Path(tmp) / "source"
        export.mkdir()
        archive_bytes = subprocess.check_output(
            ["git", "-C", str(repo), "archive", "--format=tar", head]
        )
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
            _safe_extract(archive, export)
        subprocess.run(["uv", "build", "--out-dir", str(out_dir)], cwd=export, check=True)

    wheels = sorted(out_dir.glob("*.whl"))
    sdists = sorted(out_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        fail(f"expected exactly one wheel and one sdist, got wheels={len(wheels)} sdists={len(sdists)}")

    parity = _verify_payload_parity(
        repo,
        head,
        wheels[0],
        sdists[0],
        package_name=package_name,
        version=version,
    )
    receipt = {
        "status": "PASS",
        "schemaVersion": 1,
        "source": {"commit": head, "tree": tree},
        "package": {"name": package_name, "version": version},
        "buildIsolation": "DISPOSABLE_GIT_ARCHIVE_EXACT_HEAD",
        "artifacts": {
            "wheel": {"name": wheels[0].name, "sha256": sha256_file(wheels[0])},
            "sdist": {"name": sdists[0].name, "sha256": sha256_file(sdists[0])},
        },
        "payloadParity": parity,
        "releaseVersionAssignedByThisGate": False,
        "releaseNameAssignedByThisGate": False,
        "publicationOrDeploymentPerformed": False,
    }
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.receipt:
        receipt_path = Path(args.receipt).resolve()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
