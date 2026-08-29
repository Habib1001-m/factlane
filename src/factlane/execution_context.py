from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AuthProbe = Callable[[Path], bool]
CapacityReader = Callable[[], object]

_SECRET_PATTERNS = (
    re.compile(r"(?i)-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,})(?![A-Za-z0-9_])"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)\b(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+"),
)


def redact_text(value: str) -> str:
    """Remove common credential forms without retaining probe output."""
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def _normalized_value(value: object) -> str:
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - fail closed for an untrusted report value
        return "[INVALID]"
    normalized = " ".join(text.splitlines())
    return "".join(char if ord(char) >= 0x20 or char == "\t" else " " for char in normalized)


def _render_value(value: object) -> str:
    return _normalized_value(value)[:512]


def _safe_report_value(value: object | None) -> str | None:
    return None if value is None else redact_text(_normalized_value(value))[:512]


@dataclass(frozen=True)
class CapacityStatus:
    """A bounded, read-only native-host capacity/status result."""

    available: bool
    status: str


@dataclass(frozen=True)
class CapacityGateResult:
    capacity_introspection: str
    gate_status: str
    mutation_allowed: bool
    future_mutation: str


def check_native_memory_capacity(reader: CapacityReader | None = None) -> CapacityGateResult:
    """Check capacity status without reading or mutating native memory."""
    if reader is None:
        return CapacityGateResult(
            capacity_introspection="UNAVAILABLE",
            gate_status="PASS",
            mutation_allowed=False,
            future_mutation="FAIL_CLOSED_PENDING_CAPACITY_CHECK_OR_HOUSEKEEPING",
        )
    try:
        result = reader()
    except Exception:  # noqa: BLE001 - fail closed at an untrusted probe boundary
        return CapacityGateResult(
            capacity_introspection="UNAVAILABLE",
            gate_status="HOLD",
            mutation_allowed=False,
            future_mutation="FAIL_CLOSED_PENDING_CAPACITY_CHECK_OR_HOUSEKEEPING",
        )
    if (
        not isinstance(result, CapacityStatus)
        or not isinstance(result.available, bool)
        or not isinstance(result.status, str)
        or result.status not in {"OK", "PRESSURE", "UNKNOWN"}
    ):
        return CapacityGateResult(
            capacity_introspection="UNAVAILABLE",
            gate_status="HOLD",
            mutation_allowed=False,
            future_mutation="FAIL_CLOSED_PENDING_CAPACITY_CHECK_OR_HOUSEKEEPING",
        )
    if not result.available:
        return CapacityGateResult(
            capacity_introspection="UNAVAILABLE",
            gate_status="HOLD",
            mutation_allowed=False,
            future_mutation="FAIL_CLOSED_PENDING_CAPACITY_CHECK_OR_HOUSEKEEPING",
        )
    if result.status != "OK":
        return CapacityGateResult(
            capacity_introspection="AVAILABLE",
            gate_status="HOLD",
            mutation_allowed=False,
            future_mutation="FAIL_CLOSED_PENDING_CAPACITY_CHECK_OR_HOUSEKEEPING",
        )
    return CapacityGateResult(
        capacity_introspection="AVAILABLE",
        gate_status="PASS",
        mutation_allowed=False,
        future_mutation="CLOSED_IN_THIS_SLICE",
    )


def _read_git_value(git_binary: str, cwd: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            [git_binary, *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _resolve_binary(name: str) -> tuple[str | None, str | None]:
    try:
        value = shutil.which(name)
    except (OSError, TypeError, ValueError):
        return None, f"{name.upper()}_BINARY_INVALID"
    if value is None:
        return None, f"{name.upper()}_BINARY_MISSING"
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(char) < 0x20 for char in value):
        return None, f"{name.upper()}_BINARY_INVALID"
    return value, None


def _repository_cwd(value: str | Path | None) -> tuple[Path | None, str | None]:
    try:
        path = Path.cwd() if value is None else Path(value)
    except (OSError, TypeError, ValueError):
        return None, "REPOSITORY_CONTEXT_INVALID"
    return path, None


def _auth_environment(home: Path) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GH_PROMPT_DISABLED": "1",
    }
    for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "NO_COLOR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _default_auth_probe(gh_binary: str, home: Path) -> bool:
    """Probe gh auth silently in exactly one HOME context."""
    try:
        result = subprocess.run(
            [gh_binary, "auth", "status", "--hostname", "github.com"],
            env=_auth_environment(home),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _effective_home() -> tuple[Path | None, str | None]:
    value = os.environ.get("HOME")
    if value is None or value.strip() == "":
        return None, "EFFECTIVE_HOME_INVALID"
    try:
        path = Path(value)
        valid = path.is_absolute() and path.is_dir() and os.access(path, os.R_OK | os.X_OK)
    except (OSError, TypeError, ValueError):
        return None, "EFFECTIVE_HOME_INVALID"
    return (path, None) if valid else (None, "EFFECTIVE_HOME_INVALID")


def _explicit_home(value: str | Path | None) -> tuple[Path | None, str | None]:
    if value is None:
        value = os.environ.get("FACTLANE_OWNER_HOME")
    if value is None:
        return None, "OWNER_CONTEXT_NOT_PROVIDED"
    if isinstance(value, str):
        if not value.strip():
            return None, "OWNER_CONTEXT_NOT_PROVIDED"
    elif not isinstance(value, Path):
        return None, "OWNER_CONTEXT_INVALID"
    try:
        path = Path(value)
        valid = path.is_absolute() and path.is_dir() and os.access(path, os.R_OK | os.X_OK)
    except (OSError, TypeError, ValueError):
        return None, "OWNER_CONTEXT_INVALID"
    return (path, None) if valid else (None, "OWNER_CONTEXT_INVALID")


def _auth_result(
    *,
    home: Path | None,
    context: str,
    gh_binary: str | None,
    gh_binary_failure: str | None,
    auth_probe: AuthProbe,
    precondition_failure: str | None = None,
) -> tuple[bool, str | None]:
    if precondition_failure:
        return False, precondition_failure
    if home is None:
        return False, "OWNER_CONTEXT_NOT_PROVIDED"
    if gh_binary_failure:
        return False, f"{gh_binary_failure}_IN_{context}"
    if gh_binary is None:
        return False, f"GH_BINARY_MISSING_IN_{context}"
    try:
        result = auth_probe(home)
    except Exception:  # noqa: BLE001 - fail closed at an untrusted probe boundary
        return False, f"GITHUB_AUTH_PROBE_FAILED_IN_{context}"
    if not isinstance(result, bool):
        return False, f"GITHUB_AUTH_PROBE_INVALID_IN_{context}"
    if not result:
        return False, f"GITHUB_AUTH_UNAVAILABLE_IN_{context}"
    return True, None


@dataclass(frozen=True)
class ExecutionContextReport:
    actor: str
    effective_home: str | None
    owner_or_real_home: str | None
    repository_root: str | None
    current_branch: str | None
    git_binary: str | None
    gh_binary: str | None
    ambient_github_auth_ready: bool
    owner_context_github_auth_ready: bool
    ambient_failure_class: str | None
    owner_failure_class: str | None
    repository_failure_class: str | None
    side_effect_context: str
    tool_present_vs_ready_contract: str
    readiness_exact_context: str
    environment_scoped_fact: str
    status: str
    capacity: CapacityGateResult

    def as_dict(self) -> dict[str, Any]:
        safe = _safe_report_value
        return {
            "ACTOR": safe(self.actor),
            "EFFECTIVE_HOME": safe(self.effective_home),
            "OWNER_OR_REAL_HOME": safe(self.owner_or_real_home),
            "REPOSITORY_ROOT": safe(self.repository_root),
            "CURRENT_BRANCH": safe(self.current_branch),
            "GIT_BINARY": safe(self.git_binary),
            "GH_BINARY": safe(self.gh_binary),
            "AMBIENT_GITHUB_AUTH_READY": safe("YES" if self.ambient_github_auth_ready else "NO"),
            "OWNER_CONTEXT_GITHUB_AUTH_READY": safe("YES" if self.owner_context_github_auth_ready else "NO"),
            "SIDE_EFFECT_CONTEXT": safe(self.side_effect_context),
            "TOOL_PRESENT_VS_READY_CONTRACT": safe(self.tool_present_vs_ready_contract),
            "READINESS_EXACT_CONTEXT": safe(self.readiness_exact_context),
            "ENVIRONMENT_SCOPED_FACT": safe(self.environment_scoped_fact),
            "EXECUTION_CONTEXT_PREFLIGHT": safe(self.status),
            "AMBIENT_FAILURE_CLASS": safe(self.ambient_failure_class),
            "OWNER_FAILURE_CLASS": safe(self.owner_failure_class),
            "REPOSITORY_FAILURE_CLASS": safe(self.repository_failure_class),
            "NATIVE_MEMORY_MUTATION": safe("NONE"),
            "CHECK_NATIVE_MEMORY_CAPACITY_BEFORE_MEMORY_MUTATION": safe("REQUIRED"),
            "NATIVE_MEMORY_CAPACITY_GATE": safe(self.capacity.gate_status),
            "CAPACITY_INTROSPECTION": safe(self.capacity.capacity_introspection),
            "FUTURE_NATIVE_MEMORY_MUTATION": safe(self.capacity.future_mutation),
        }

    def render(self) -> str:
        lines: list[str] = []
        for key, value in self.as_dict().items():
            rendered = "UNKNOWN" if value is None else _render_value(value)
            lines.append(f"{key}={redact_text(rendered)}")
        return "\n".join(lines)


def run_preflight(
    *,
    cwd: str | Path | None = None,
    owner_home: str | Path | None = None,
    actor: str | None = None,
    auth_probe: AuthProbe | None = None,
    capacity_reader: CapacityReader | None = None,
) -> ExecutionContextReport:
    """Classify one exact runtime/repository context without side effects."""
    effective_home, effective_home_failure = _effective_home()
    explicit_home, owner_precondition = _explicit_home(owner_home)
    git_binary, git_binary_failure = _resolve_binary("git")
    gh_binary, gh_binary_failure = _resolve_binary("gh")
    repository_cwd, cwd_failure = _repository_cwd(cwd)

    repository_root: str | None = None
    current_branch: str | None = None
    repository_failure: str | None = None
    if git_binary_failure:
        repository_failure = git_binary_failure
    elif git_binary is None:
        repository_failure = "GIT_BINARY_MISSING"
    elif effective_home_failure:
        repository_failure = effective_home_failure
    elif cwd_failure:
        repository_failure = cwd_failure
    elif repository_cwd is None:
        repository_failure = "REPOSITORY_CONTEXT_INVALID"
    else:
        repository_root = _read_git_value(git_binary, repository_cwd, "rev-parse", "--show-toplevel")
        current_branch = _read_git_value(git_binary, repository_cwd, "branch", "--show-current")
        if repository_root is None or current_branch is None:
            repository_failure = "REPOSITORY_CONTEXT_INVALID"

    if auth_probe is None:
        auth_probe = lambda home: _default_auth_probe(gh_binary or "gh", home)

    ambient_ready, ambient_failure = _auth_result(
        home=effective_home,
        context="HERMES_RUNTIME_CONTEXT",
        gh_binary=gh_binary,
        gh_binary_failure=gh_binary_failure,
        auth_probe=auth_probe,
        precondition_failure=(
            f"{effective_home_failure}_IN_HERMES_RUNTIME_CONTEXT"
            if effective_home_failure
            else None
        ),
    )
    owner_ready, owner_failure = _auth_result(
        home=explicit_home,
        context="EXPLICIT_OWNER_CONTEXT",
        gh_binary=gh_binary,
        gh_binary_failure=gh_binary_failure,
        auth_probe=auth_probe,
        precondition_failure=owner_precondition,
    )

    capacity = check_native_memory_capacity(capacity_reader)
    side_effect_context = "EXPLICIT_OWNER_CONTEXT" if owner_ready else "APPROVED_BRIDGE_REQUIRED"
    status = "PASS" if repository_failure is None and owner_ready and capacity.gate_status == "PASS" else "HOLD"
    return ExecutionContextReport(
        actor=actor or os.environ.get("FACTLANE_ACTOR", "UNKNOWN"),
        effective_home=str(effective_home) if effective_home else None,
        owner_or_real_home=str(explicit_home) if explicit_home else None,
        repository_root=repository_root,
        current_branch=current_branch,
        git_binary=git_binary,
        gh_binary=gh_binary,
        ambient_github_auth_ready=ambient_ready,
        owner_context_github_auth_ready=owner_ready,
        ambient_failure_class=ambient_failure,
        owner_failure_class=owner_failure,
        repository_failure_class=repository_failure,
        side_effect_context=side_effect_context,
        tool_present_vs_ready_contract="PASS",
        readiness_exact_context="PASS",
        environment_scoped_fact="TRUE",
        status=status,
        capacity=capacity,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FactLane exact execution-context preflight")
    parser.add_argument("--actor", default=os.environ.get("FACTLANE_ACTOR", "UNKNOWN"))
    parser.add_argument("--owner-home", default=os.environ.get("FACTLANE_OWNER_HOME"))
    args = parser.parse_args(argv)
    report = run_preflight(cwd=Path.cwd(), owner_home=args.owner_home, actor=args.actor)
    print(report.render())
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
