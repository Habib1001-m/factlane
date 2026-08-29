from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from factlane import execution_context
from factlane.execution_context import (
    CapacityStatus,
    check_native_memory_capacity,
    redact_text,
    run_preflight,
)

SECRET = "ghp_" + "a" * 36


@pytest.fixture(autouse=True)
def fake_tool_presence(monkeypatch) -> None:
    monkeypatch.setattr(execution_context.shutil, "which", lambda name: name if name in {"git", "gh"} else None)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "switch", "-c", "test-entry-preflight"], check=True, capture_output=True, text=True)
    return repo


def test_report_distinguishes_effective_and_explicit_owner_home(tmp_path, monkeypatch) -> None:
    runtime_home = tmp_path / "runtime-home"
    owner_home = tmp_path / "owner-home"
    runtime_home.mkdir()
    owner_home.mkdir()
    monkeypatch.setenv("HOME", str(runtime_home))

    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=owner_home,
        actor="HERMES",
        auth_probe=lambda home: Path(home) == owner_home,
    )

    assert report.actor == "HERMES"
    assert report.effective_home == str(runtime_home)
    assert report.owner_or_real_home == str(owner_home)
    assert report.ambient_github_auth_ready is False
    assert report.owner_context_github_auth_ready is True
    assert "AMBIENT_GITHUB_AUTH_READY=NO" in report.render()
    assert "OWNER_CONTEXT_GITHUB_AUTH_READY=YES" in report.render()


def test_same_binary_can_have_different_context_readiness(tmp_path) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()

    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=owner_home,
        auth_probe=lambda home: Path(home) == owner_home,
    )

    assert report.git_binary == "git"
    assert report.gh_binary == "gh"
    assert report.tool_present_vs_ready_contract == "PASS"
    assert report.ambient_github_auth_ready is False
    assert report.owner_context_github_auth_ready is True


def test_auth_failure_is_scoped_to_its_execution_context(tmp_path) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()

    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=owner_home,
        auth_probe=lambda _home: False,
    )

    assert report.ambient_failure_class == "GITHUB_AUTH_UNAVAILABLE_IN_HERMES_RUNTIME_CONTEXT"
    assert report.owner_failure_class == "GITHUB_AUTH_UNAVAILABLE_IN_EXPLICIT_OWNER_CONTEXT"
    assert report.side_effect_context == "APPROVED_BRIDGE_REQUIRED"
    assert report.status == "HOLD"


def test_report_redacts_secret_material_and_probe_output(tmp_path) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()

    assert SECRET not in redact_text(f"Authorization: Bearer {SECRET} token={SECRET}")

    def failing_probe(_home):
        raise RuntimeError(f"probe output leaked {SECRET}")

    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=owner_home,
        actor=SECRET,
        auth_probe=failing_probe,
    )

    rendered = report.render()
    assert SECRET not in rendered
    assert SECRET not in str(report.as_dict())
    assert "probe output leaked" not in rendered
    assert report.ambient_failure_class == "GITHUB_AUTH_PROBE_FAILED_IN_HERMES_RUNTIME_CONTEXT"


def test_safe_serialization_is_bounded_before_output(tmp_path) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()
    oversized_actor = "prefix-" + "x" * 500 + SECRET + "-suffix"

    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=owner_home,
        actor=oversized_actor,
        auth_probe=lambda _home: True,
    )

    actor = report.as_dict()["ACTOR"]
    assert isinstance(actor, str)
    assert len(actor) <= 512
    assert SECRET not in actor
    assert "ghp_" not in actor


def test_missing_or_invalid_owner_context_fails_closed(tmp_path) -> None:
    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=tmp_path / "missing-owner-home",
        auth_probe=lambda _home: True,
    )

    assert report.owner_context_github_auth_ready is False
    assert report.owner_or_real_home is None
    assert report.owner_failure_class == "OWNER_CONTEXT_INVALID"
    assert report.side_effect_context == "APPROVED_BRIDGE_REQUIRED"
    assert report.status == "HOLD"

    missing_report = run_preflight(cwd=make_repo(tmp_path), owner_home=None, auth_probe=lambda _home: True)
    assert missing_report.owner_failure_class == "OWNER_CONTEXT_NOT_PROVIDED"
    assert missing_report.side_effect_context == "APPROVED_BRIDGE_REQUIRED"
    assert missing_report.status == "HOLD"


def test_missing_repository_context_fails_closed(tmp_path) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()

    report = run_preflight(
        cwd=tmp_path / "not-a-repository",
        owner_home=owner_home,
        auth_probe=lambda _home: True,
    )

    assert report.repository_root is None
    assert report.current_branch is None
    assert report.repository_failure_class == "REPOSITORY_CONTEXT_INVALID"
    assert report.status == "HOLD"


def test_missing_effective_home_fails_closed(tmp_path, monkeypatch) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()
    repo = make_repo(tmp_path)
    monkeypatch.delenv("HOME", raising=False)

    report = run_preflight(
        cwd=repo,
        owner_home=owner_home,
        auth_probe=lambda _home: True,
    )

    assert report.effective_home is None
    assert report.ambient_failure_class == "EFFECTIVE_HOME_INVALID_IN_HERMES_RUNTIME_CONTEXT"
    assert report.status == "HOLD"


def test_null_byte_repository_context_fails_closed(tmp_path) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()

    report = run_preflight(
        cwd="\x00",
        owner_home=owner_home,
        auth_probe=lambda _home: True,
    )

    assert report.repository_root is None
    assert report.current_branch is None
    assert report.repository_failure_class == "REPOSITORY_CONTEXT_INVALID"
    assert report.status == "HOLD"


def test_null_byte_home_contexts_fail_closed(tmp_path, monkeypatch) -> None:
    repo = make_repo(tmp_path)

    monkeypatch.setattr(execution_context.os, "environ", {"HOME": f"{tmp_path}{chr(0)}runtime"})
    report = run_preflight(
        cwd=repo,
        owner_home=f"{tmp_path}{chr(0)}owner",
        auth_probe=lambda _home: True,
    )

    assert report.effective_home is None
    assert report.ambient_failure_class == "EFFECTIVE_HOME_INVALID_IN_HERMES_RUNTIME_CONTEXT"
    assert report.owner_failure_class == "OWNER_CONTEXT_INVALID"
    assert chr(0) not in report.render()
    assert report.status == "HOLD"


def test_invalid_repository_cwd_type_fails_closed(tmp_path) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()

    report = run_preflight(
        cwd=object(),  # type: ignore[arg-type]
        owner_home=owner_home,
        auth_probe=lambda _home: True,
    )

    assert report.repository_root is None
    assert report.current_branch is None
    assert report.repository_failure_class == "REPOSITORY_CONTEXT_INVALID"
    assert report.status == "HOLD"


def test_invalid_owner_object_fails_closed(tmp_path) -> None:
    class InvalidOwner:
        def __str__(self):
            raise RuntimeError("invalid owner string")

    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=InvalidOwner(),  # type: ignore[arg-type]
        auth_probe=lambda _home: True,
    )

    assert report.owner_or_real_home is None
    assert report.owner_failure_class == "OWNER_CONTEXT_INVALID"
    assert report.side_effect_context == "APPROVED_BRIDGE_REQUIRED"
    assert report.status == "HOLD"


def test_default_auth_probe_uses_exact_home_without_auth_env(monkeypatch, tmp_path) -> None:
    import factlane.execution_context as module

    observed = {}
    monkeypatch.setenv("GH_TOKEN", "ambient-token-not-used")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "ambient-config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "ssh-agent.sock"))

    class Result:
        returncode = 0

    def fake_run(arguments, **kwargs):
        observed["arguments"] = arguments
        observed.update(kwargs)
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module._default_auth_probe("gh", tmp_path)
    assert observed["arguments"] == ["gh", "auth", "status", "--hostname", "github.com"]
    assert observed["env"]["HOME"] == str(tmp_path)
    assert "GH_TOKEN" not in observed["env"]
    assert "GITHUB_TOKEN" not in observed["env"]
    assert "GH_CONFIG_DIR" not in observed["env"]
    assert "XDG_CONFIG_HOME" not in observed["env"]
    assert "GIT_CONFIG_GLOBAL" not in observed["env"]
    assert "SSH_AUTH_SOCK" not in observed["env"]
    assert observed["stdout"] is module.subprocess.DEVNULL
    assert observed["stderr"] is module.subprocess.DEVNULL


def test_missing_gh_binary_fails_closed_even_with_injected_probe(tmp_path, monkeypatch) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()
    git_binary = execution_context.shutil.which("git")
    monkeypatch.setattr(execution_context.shutil, "which", lambda name: git_binary if name == "git" else None)

    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=owner_home,
        auth_probe=lambda _home: True,
    )

    assert report.gh_binary is None
    assert report.owner_context_github_auth_ready is False
    assert report.owner_failure_class == "GH_BINARY_MISSING_IN_EXPLICIT_OWNER_CONTEXT"
    assert report.side_effect_context == "APPROVED_BRIDGE_REQUIRED"
    assert report.status == "HOLD"


def test_invalid_gh_binary_fails_closed_even_with_injected_probe(tmp_path, monkeypatch) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()
    git_binary = execution_context.shutil.which("git")
    monkeypatch.setattr(execution_context.shutil, "which", lambda name: git_binary if name == "git" else f"gh{chr(0)}invalid")

    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=owner_home,
        auth_probe=lambda _home: True,
    )

    assert report.gh_binary is None
    assert report.owner_context_github_auth_ready is False
    assert report.owner_failure_class == "GH_BINARY_INVALID_IN_EXPLICIT_OWNER_CONTEXT"
    assert report.side_effect_context == "APPROVED_BRIDGE_REQUIRED"
    assert report.status == "HOLD"


def test_native_memory_gate_is_read_only_and_fail_closed_when_unavailable() -> None:
    result = check_native_memory_capacity()

    assert result.capacity_introspection == "UNAVAILABLE"
    assert result.gate_status == "PASS"
    assert result.mutation_allowed is False
    assert result.future_mutation == "FAIL_CLOSED_PENDING_CAPACITY_CHECK_OR_HOUSEKEEPING"


def test_available_capacity_status_does_not_authorize_native_write() -> None:
    result = check_native_memory_capacity(lambda: CapacityStatus(available=True, status="OK"))

    assert result.capacity_introspection == "AVAILABLE"
    assert result.gate_status == "PASS"
    assert result.mutation_allowed is False
    assert result.future_mutation == "CLOSED_IN_THIS_SLICE"


def test_capacity_pressure_holds_and_remains_closed() -> None:
    result = check_native_memory_capacity(lambda: CapacityStatus(available=True, status="PRESSURE"))

    assert result.capacity_introspection == "AVAILABLE"
    assert result.gate_status == "HOLD"
    assert result.mutation_allowed is False
    assert result.future_mutation == "FAIL_CLOSED_PENDING_CAPACITY_CHECK_OR_HOUSEKEEPING"


def test_invalid_capacity_status_holds_and_remains_closed() -> None:
    result = check_native_memory_capacity(lambda: object())

    assert result.capacity_introspection == "UNAVAILABLE"
    assert result.gate_status == "HOLD"
    assert result.mutation_allowed is False
    assert result.future_mutation == "FAIL_CLOSED_PENDING_CAPACITY_CHECK_OR_HOUSEKEEPING"


def test_unhashable_capacity_status_holds_and_remains_closed() -> None:
    result = check_native_memory_capacity(lambda: CapacityStatus(available=True, status=[]))  # type: ignore[arg-type]

    assert result.capacity_introspection == "UNAVAILABLE"
    assert result.gate_status == "HOLD"
    assert result.mutation_allowed is False


def test_render_neutralizes_multiline_report_values(tmp_path) -> None:
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()

    report = run_preflight(
        cwd=make_repo(tmp_path),
        owner_home=owner_home,
        actor="HERMES\nINJECTED=TRUE",
        auth_probe=lambda _home: True,
    )

    rendered = report.render()
    assert "ACTOR=HERMES INJECTED=TRUE" in rendered
    assert "\nINJECTED=TRUE" not in rendered
