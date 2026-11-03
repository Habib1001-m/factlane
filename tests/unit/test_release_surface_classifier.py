from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_release_surface.py"
SPEC = importlib.util.spec_from_file_location("check_release_surface", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
validate_snapshot_authority_lineage = MODULE.validate_snapshot_authority_lineage


def test_surface_snapshot_may_trail_newer_repository_package_tag() -> None:
    validate_snapshot_authority_lineage("v0.1.4", ["v0.1.6", "v0.1.5", "v0.1.4", "v0.1.3"])


def test_surface_snapshot_tag_must_remain_reachable() -> None:
    with pytest.raises(
        SystemExit,
        match=r"^HOLD: released-contract snapshot authority tag is not reachable from source: v0\.1\.4$",
    ):
        validate_snapshot_authority_lineage("v0.1.4", ["v0.1.6", "v0.1.5", "v0.1.3"])
