#!/usr/bin/env python3
"""Classify repository package identity against the current released authority.

This is a release-surface gate, not a release action. It never creates tags,
changes versions, builds artifacts, or grants publication/Production authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib


DEVELOPMENT_QUALIFICATION_ONLY = "DEVELOPMENT_QUALIFICATION_ONLY"
DEVELOPMENT_PENDING_RELEASE_AUTHORITY = "DEVELOPMENT_PENDING_RELEASE_AUTHORITY"
CLASSIFICATIONS = {
    DEVELOPMENT_QUALIFICATION_ONLY,
    DEVELOPMENT_PENDING_RELEASE_AUTHORITY,
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message: str) -> None:
    raise SystemExit(f"HOLD: {message}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expected-classification",
        required=True,
        choices=sorted(CLASSIFICATIONS),
    )
    parser.add_argument(
        "--snapshot",
        required=True,
        help="Repository-relative released-contract snapshot used as released authority.",
    )
    parser.add_argument("--receipt")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    pyproject_path = repo / "pyproject.toml"
    released_snapshot_path = (repo / args.snapshot).resolve()
    try:
        released_snapshot_path.relative_to(repo.resolve())
    except ValueError:
        fail("--snapshot must resolve inside the repository")
    if not released_snapshot_path.is_file():
        fail(f"released-contract snapshot does not exist: {args.snapshot}")

    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    package_version = pyproject["project"]["version"]
    snapshot = json.loads(released_snapshot_path.read_text(encoding="utf-8"))
    if snapshot.get("schemaVersion") != 1:
        fail("unsupported released-contract schema")
    authority = snapshot.get("releaseAuthority")
    if not isinstance(authority, dict):
        fail("released-contract snapshot is missing releaseAuthority")

    tag = authority["tag"]
    tag_ref = f"refs/tags/{tag}"
    if git(repo, "cat-file", "-t", tag_ref) != "tag":
        fail(f"released authority tag is not annotated: {tag}")
    if git(repo, "rev-parse", tag_ref) != authority["tagObject"]:
        fail(f"released tag object drift: {tag}")
    if git(repo, "rev-parse", f"{tag}^{{commit}}") != authority["commit"]:
        fail(f"released commit drift: {tag}")
    if git(repo, "rev-parse", f"{tag}^{{tree}}") != authority["tree"]:
        fail(f"released tree drift: {tag}")

    released_pyproject = tomllib.loads(git(repo, "show", f"{tag}:pyproject.toml"))
    released_package_name = released_pyproject["project"]["name"]
    released_version = released_pyproject["project"]["version"]
    if released_version != authority["packageVersion"]:
        fail(
            "released snapshot packageVersion does not match the exact tagged pyproject: "
            f"{authority['packageVersion']} != {released_version}"
        )

    worktree_status = git(repo, "status", "--porcelain")
    if worktree_status:
        fail("release-surface classification requires a fully clean worktree")

    reachable_tags = [
        value
        for value in git(repo, "tag", "--merged", "HEAD", "--sort=-version:refname", "--list", "v*").splitlines()
        if re.fullmatch(r"v\d+\.\d+\.\d+", value)
    ]
    if not reachable_tags:
        fail("no released tag is reachable from the source")
    latest_reachable_release_tag = reachable_tags[0]
    if latest_reachable_release_tag != tag:
        fail(
            "released-contract snapshot is stale: "
            f"latest reachable tag is {latest_reachable_release_tag}, snapshot authority is {tag}"
        )

    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    exact_released_source = head == authority["commit"] and tree == authority["tree"]
    if exact_released_source:
        fail(
            "exact released authority source is outside this Development-only gate; "
            "verify released artifacts against the release manifest instead"
        )

    if pyproject["project"]["name"] == released_package_name and package_version == released_version:
        classification = DEVELOPMENT_QUALIFICATION_ONLY
        reason = "DEVELOPMENT_SOURCE_REUSES_RELEASED_PACKAGE_VERSION_METADATA"
    else:
        classification = DEVELOPMENT_PENDING_RELEASE_AUTHORITY
        reason = "DEVELOPMENT_PACKAGE_IDENTITY_DIFFERS_FROM_CURRENT_RELEASED_AUTHORITY"

    receipt = {
        "status": "PASS" if classification == args.expected_classification else "HOLD",
        "schemaVersion": 1,
        "classification": classification,
        "expectedClassification": args.expected_classification,
        "reason": reason,
        "source": {"commit": head, "tree": tree},
        "package": {"name": pyproject["project"]["name"], "version": package_version},
        "releasedAuthority": {
            **authority,
            "latestReachableReleaseTag": latest_reachable_release_tag,
            "snapshotSha256": sha256_file(released_snapshot_path),
            "taggedPackageName": released_package_name,
            "taggedPackageVersion": released_version,
        },
        "releaseIdentityEligible": False,
        "distributionRule": (
            "Only artifacts bound to the exact released tag/commit/tree and recorded published "
            "digests are the official release. Artifacts built from other source bytes are "
            "Development/qualification-only until separate release identity is assigned and "
            "accepted."
        ),
        "releaseVersionAssignedByThisGate": False,
        "releaseNameAssignedByThisGate": False,
        "publicationOrDeploymentPerformed": False,
    }

    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.receipt:
        receipt_path = Path(args.receipt).resolve()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)

    if classification != args.expected_classification:
        fail(
            f"release-surface classification is {classification}, expected "
            f"{args.expected_classification}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
