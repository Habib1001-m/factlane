from __future__ import annotations

import io
import json
import os
import tomllib
from pathlib import Path
from typing import Any, Mapping

import pytest

from factlane import cli
from factlane.webui_install import (
    BrowserInstall,
    EndpointState,
    InstallEnvironment,
    InstallerStepStatus,
    SetupApplyResult,
    SetupConsent,
    SetupRequest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _setup_configuration() -> cli.SetupConfiguration:
    return cli.SetupConfiguration(
        request=SetupRequest(
            installation_id="abc123",
            saved_port=8768,
            lingering_required=True,
            installing_user="alice",
            factlane_executable="/home/alice/.local/bin/factlane",
            owner_config_home="/home/alice/.config",
            service_config_path="/home/alice/.config/factlane/webui.json",
            caddy_config_path="/home/alice/.config/factlane/Caddyfile",
            user_unit_path="/home/alice/.config/systemd/user/factlane-webui.service",
            backend_socket="/run/user/1000/factlane/webui.sock",
            runtime_dir="/run/user/1000/factlane",
            state_dir="/home/alice/.local/state/factlane",
            caddy_state_dir="/home/alice/.local/state/factlane/caddy",
            browser_profile_dir="/home/alice/.local/state/factlane/browser",
            db=None,
            profile=None,
            host_id=None,
        ),
        consent=SetupConsent(
            hostname_mapping=True,
            browser_profile_trust=True,
            user_lingering=True,
        ),
        environment=InstallEnvironment(
            system="Linux",
            systemd=True,
            caddy_path="/usr/bin/caddy",
            zenity_path="/usr/bin/zenity",
            browser=BrowserInstall(
                family="firefox",
                executable="/usr/bin/firefox",
            ),
            euid=1000,
        ),
    )


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--db", "state.db", "--host-id", "agent-a"],
        ["--help-tools"],
        ["setup"],
        ["setup", "webui", "--apply"],
        ["setup", "webui", "-h"],
        ["setup", "other"],
        ["doctor"],
        ["doctor", "webui", "--json"],
        ["doctor", "WEBUI"],
        ["launch"],
        ["launch", "webui", "--verbose"],
        ["launch", "WEBUI"],
        ["serve", "webui", "--config", "/tmp/webui.json"],
        ["status", "webui", "--verbose"],
        ["unknown", "webui"],
        ["webui", "status"],
    ],
)
def test_every_non_exact_webui_form_delegates_legacy_argv_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    calls: list[list[str]] = []

    def legacy(received: list[str] | None = None) -> int:
        assert received is not None
        calls.append(received)
        return 73

    monkeypatch.setattr(cli.server, "main", legacy)

    result = cli.main(argv)

    assert result == 73
    assert calls == [argv]


@pytest.mark.parametrize(
    "action",
    ["setup", "doctor", "launch", "serve", "open", "start", "stop", "status", "restart"],
)
def test_only_exact_supported_webui_pairs_are_recognized(action: str) -> None:
    parsed = cli.parse_webui_command([action, "webui"])

    assert parsed is not None
    assert parsed.action == action
    assert cli.parse_webui_command([action, "webui", "extra"]) is None
    assert cli.parse_webui_command([action.upper(), "webui"]) is None
    assert cli.parse_webui_command([action, "WEBUI"]) is None


def test_top_level_help_exposes_managed_webui_and_preserves_legacy_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def legacy(received: list[str] | None = None) -> int:
        assert received is not None
        calls.append(received)
        print("legacy stdio help: --db DB --host-id HOST_ID --help-tools")
        return 0

    monkeypatch.setattr(cli.server, "main", legacy)
    output = io.StringIO()

    result = cli.main(["--help"], output=output)

    rendered = output.getvalue()
    assert result == 0
    assert calls == [["--help"]]
    for action in (
        "setup",
        "doctor",
        "launch",
        "serve",
        "open",
        "start",
        "stop",
        "status",
        "restart",
    ):
        assert f"factlane {action} webui" in rendered
    assert "--db DB" in rendered
    assert "--host-id HOST_ID" in rendered
    assert "--help-tools" in rendered


@pytest.mark.parametrize(
    "action",
    ["setup", "doctor", "launch", "serve", "open", "start", "stop", "status", "restart"],
)
def test_managed_webui_help_is_exact_and_has_no_execution_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    def legacy(_received: list[str] | None = None) -> int:
        pytest.fail("managed WebUI --help must not enter the legacy parser")

    def setup_provider() -> cli.SetupConfiguration | None:
        pytest.fail("managed WebUI --help must not compose setup configuration")

    class Controller:
        def dispatch(self, _requested: str) -> dict[str, object]:
            pytest.fail("managed WebUI --help must not dispatch service control")

    monkeypatch.setattr(cli.server, "main", legacy)
    output = io.StringIO()

    result = cli.main(
        [action, "webui", "--help"],
        setup_config_provider=setup_provider,
        service_controller=Controller(),
        output=output,
    )

    rendered = output.getvalue()
    assert result == 0
    assert f"usage: factlane {action} webui" in rendered
    assert "Managed WebUI command" in rendered


def test_setup_webui_calls_pure_planner_and_emits_structured_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, Any, Any]] = []
    original = cli.plan_webui_setup

    def planner(request: Any, environment: Any, consent: Any) -> Any:
        calls.append((request, environment, consent))
        return original(request, environment, consent)

    monkeypatch.setattr(cli, "plan_webui_setup", planner)
    output = io.StringIO()
    configuration = _setup_configuration()

    result = cli.main(
        ["setup", "webui"],
        setup_config_provider=lambda: configuration,
        setup_executor=lambda _receipt, _consent: {
            "status": "APPLIED",
            "ok": True,
        },
        output=output,
    )

    payload = json.loads(output.getvalue())
    assert result == 0
    assert calls == [
        (
            configuration.request,
            configuration.environment,
            configuration.consent,
        )
    ]
    assert payload["command"] == "setup webui"
    assert payload["status"] == "READY_TO_APPLY"
    assert payload["apply_available"] is True
    assert payload["plan"]["hostname"] == "factlane-abc123.test"
    assert payload["plan"]["origin"] == "http://127.0.0.1:8768"
    assert payload["plan"]["operations"]
    assert payload["apply"] == {"ok": True, "status": "APPLIED"}


def test_setup_webui_emits_blocked_receipt_from_pure_planner() -> None:
    configuration = _setup_configuration()
    blocked_environment = InstallEnvironment(
        system="Linux",
        systemd=True,
        caddy_path=None,
        zenity_path="/usr/bin/zenity",
        browser=configuration.environment.browser if configuration.environment else None,
        euid=1000,
    )
    output = io.StringIO()

    result = cli.main(
        ["setup", "webui"],
        setup_config_provider=lambda: cli.SetupConfiguration(
            request=configuration.request,
            consent=configuration.consent,
            environment=blocked_environment,
        ),
        output=output,
    )

    payload = json.loads(output.getvalue())
    assert result == 1
    assert payload["status"] == "BLOCKED"
    assert payload["plan"] is None
    assert payload["apply_available"] is False
    assert any("Caddy" in reason for reason in payload["blocked_reasons"])


def test_setup_default_acquires_explicit_consent_then_uses_default_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _setup_configuration()
    built: list[bool] = []
    detected: list[bool] = []
    consent_calls: list[tuple[object, object]] = []
    executor_builds: list[tuple[object, object]] = []
    executor_calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        cli,
        "build_default_setup_request",
        lambda: built.append(True) or configuration.request,
    )
    monkeypatch.setattr(
        cli,
        "detect_install_environment",
        lambda: detected.append(True) or configuration.environment,
    )
    monkeypatch.setattr(
        cli,
        "acquire_default_setup_consent",
        lambda request, environment: consent_calls.append((request, environment))
        or configuration.consent,
    )

    def build_executor(request: object, environment: object, **kwargs: object):
        assert kwargs == {"windows_setup_precondition": None}
        executor_builds.append((request, environment))

        def executor(receipt: object, consent: object) -> SetupApplyResult:
            executor_calls.append((receipt, consent))
            return SetupApplyResult(
                status=InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER,
                endpoint_state=EndpointState.PROPOSED,
                transaction_id="11111111-1111-1111-1111-111111111111",
                detail="privileged installer required",
            )

        return executor

    monkeypatch.setattr(cli, "build_default_setup_executor", build_executor)
    output = io.StringIO()

    result = cli.main(["setup", "webui"], output=output)

    payload = json.loads(output.getvalue())
    assert result == 1
    assert built == [True]
    assert detected == [True]
    assert consent_calls == [(configuration.request, configuration.environment)]
    assert executor_builds == [(configuration.request, configuration.environment)]
    assert len(executor_calls) == 1
    assert executor_calls[0][1] == configuration.consent
    assert payload["command"] == "setup webui"
    assert payload["status"] == "READY_TO_APPLY"
    assert payload["apply_available"] is True
    assert payload["apply"]["status"] == "NEEDS_PRIVILEGED_INSTALLER"
    assert payload["apply"]["endpoint_state"] == "PROPOSED"
    assert payload["apply"]["transaction_id"] == "11111111-1111-1111-1111-111111111111"


def test_default_windows_setup_captures_exact_precondition_before_native_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _setup_configuration()
    environment = InstallEnvironment(
        system="Windows",
        systemd=False,
        caddy_path=None,
        zenity_path=None,
        browser=None,
        euid=-1,
    )
    token = object()
    events: list[str] = []
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli, "detect_install_environment", lambda: environment)
    monkeypatch.setattr(cli, "build_default_setup_request", lambda: configuration.request)
    monkeypatch.setattr(
        cli,
        "capture_windows_setup_precondition",
        lambda request: events.append("capture") or token,
    )
    monkeypatch.setattr(
        cli,
        "acquire_default_setup_consent",
        lambda request, observed_environment: events.append("consent")
        or SetupConsent(False, False, False, windows_native_setup=True),
    )

    observed = cli._default_setup_configuration()

    assert events == ["capture", "consent"]
    assert observed.windows_precondition is token


def test_injected_setup_executor_receives_exact_receipt_and_consent_and_bypasses_default_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _setup_configuration()
    applied: list[tuple[object, object]] = []
    monkeypatch.setattr(
        cli,
        "build_default_setup_executor",
        lambda *_args, **_kwargs: pytest.fail("default builder must not run"),
    )

    def executor(receipt: Any, consent: Any) -> dict[str, object]:
        applied.append((receipt, consent))
        return {"status": "APPLIED", "ok": True}

    with_executor = io.StringIO()
    result = cli.main(
        ["setup", "webui"],
        setup_config_provider=lambda: configuration,
        setup_executor=executor,
        output=with_executor,
    )
    payload = json.loads(with_executor.getvalue())
    assert result == 0
    assert len(applied) == 1
    assert applied[0][0].plan is not None
    assert applied[0][0].plan.origin == "http://127.0.0.1:8768"
    assert applied[0][1] == configuration.consent
    assert payload["apply_available"] is True
    assert payload["apply"] == {"ok": True, "status": "APPLIED"}


def test_default_consent_denial_blocks_before_executor_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _setup_configuration()
    monkeypatch.setattr(cli, "build_default_setup_request", lambda: configuration.request)
    monkeypatch.setattr(
        cli, "detect_install_environment", lambda: configuration.environment
    )
    monkeypatch.setattr(
        cli,
        "acquire_default_setup_consent",
        lambda _request, _environment: SetupConsent(
            hostname_mapping=False,
            browser_profile_trust=False,
            user_lingering=False,
        ),
    )
    monkeypatch.setattr(
        cli,
        "build_default_setup_executor",
        lambda *_args, **_kwargs: pytest.fail("executor must not be built"),
    )
    output = io.StringIO()

    result = cli.main(["setup", "webui"], output=output)

    payload = json.loads(output.getvalue())
    assert result == 1
    assert payload["status"] == "BLOCKED"
    assert payload["plan"] is None
    assert payload["apply_available"] is False
    assert any("consent" in reason for reason in payload["blocked_reasons"])


@pytest.mark.parametrize("action", ["open", "start", "stop", "status", "restart"])
def test_service_actions_default_to_platform_boundary_without_shelling(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            assert requested == action
            return {"status": "ERROR", "error_code": "CONTROL_UNAVAILABLE"}

    monkeypatch.setattr(cli, "build_default_platform_controller", lambda: Controller())
    output = io.StringIO()

    result = cli.main([action, "webui"], output=output)

    payload = json.loads(output.getvalue())
    assert result == 1
    assert payload["status"] == "ERROR"
    assert payload["error_code"] == "CONTROL_UNAVAILABLE"
    assert payload["command"] == f"{action} webui"
    assert not hasattr(cli, "subprocess")


def test_serve_webui_uses_production_default_controller_and_surfaces_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            return {
                "status": "ERROR",
                "error_code": "RUNTIME_CONFIG_ERROR",
                "message": "runtime config is unavailable",
            }

    monkeypatch.setattr(cli, "build_default_platform_controller", lambda: Controller())
    output = io.StringIO()

    result = cli.main(["serve", "webui"], output=output)

    payload = json.loads(output.getvalue())
    assert result == 1
    assert calls == ["serve"]
    assert payload["status"] == "ERROR"
    assert payload["error_code"] == "RUNTIME_CONFIG_ERROR"
    assert payload["command"] == "serve webui"


@pytest.mark.parametrize("action", ["open", "start", "stop", "status", "restart"])
def test_service_actions_without_injection_use_production_default_controller(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    calls: list[str] = []

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            return {"status": "ERROR", "error_code": "CONTROL_UNAVAILABLE"}

    monkeypatch.setattr(cli, "build_default_platform_controller", lambda: Controller())
    output = io.StringIO()

    result = cli.main([action, "webui"], output=output)

    assert result == 1
    assert calls == [action]
    assert json.loads(output.getvalue())["error_code"] == "CONTROL_UNAVAILABLE"


def test_open_webui_keeps_final_json_on_output_while_controller_presents_link_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FlushTrackingStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

    stderr = FlushTrackingStream()
    url = "http://127.0.0.1:8765#connect=locator-1"

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            assert requested == "open"
            cli.sys.stderr.write(
                "Open this URL in any browser:\n"
                f"{url}\n"
                "Waiting for browser connection and local approval...\n"
            )
            cli.sys.stderr.flush()
            return {
                "status": "OK",
                "state": "OPENED",
                "origin": "http://127.0.0.1:8765",
            }

    monkeypatch.setattr(cli, "build_default_platform_controller", lambda: Controller())
    monkeypatch.setattr(cli.sys, "stderr", stderr)
    output = io.StringIO()

    result = cli.main(["open", "webui"], output=output)

    assert result == 0
    assert calls == ["open"]
    assert output.getvalue().count("\n") == 1
    assert json.loads(output.getvalue()) == {
        "command": "open webui",
        "origin": "http://127.0.0.1:8765",
        "state": "OPENED",
        "status": "OK",
    }
    assert "#connect=" not in output.getvalue()
    assert stderr.getvalue() == (
        "Open this URL in any browser:\n"
        f"{url}\n"
        "Waiting for browser connection and local approval...\n"
    )
    assert stderr.flushes == 1


@pytest.mark.parametrize("action", ["serve", "open", "start", "stop", "status", "restart"])
def test_service_actions_delegate_only_to_explicitly_injected_controller(action: str) -> None:
    calls: list[str] = []

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            return {"status": "OK", "ok": True, "action": requested}

    output = io.StringIO()
    result = cli.main(
        [action, "webui"],
        service_controller=Controller(),
        output=output,
    )

    assert result == 0
    assert calls == [action]
    assert json.loads(output.getvalue()) == {
        "action": action,
        "command": f"{action} webui",
        "ok": True,
        "status": "OK",
    }


def test_launch_webui_already_ready_dispatches_status_then_open() -> None:
    calls: list[str] = []

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            if requested == "status":
                return {
                    "status": "OK",
                    "unit": {"active": True},
                    "service": {"status": "READY"},
                }
            assert requested == "open"
            return {"status": "OK", "ok": True, "opened": True}

    output = io.StringIO()
    result = cli.main(
        ["launch", "webui"],
        service_controller=Controller(),
        output=output,
    )

    assert result == 0
    assert calls == ["status", "open"]
    assert json.loads(output.getvalue()) == {
        "command": "launch webui",
        "ok": True,
        "opened": True,
        "status": "OK",
    }


def test_launch_webui_inactive_dispatches_status_start_then_open() -> None:
    calls: list[str] = []

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            if requested == "status":
                return {
                    "status": "OK",
                    "unit": {"active": False},
                    "service": {"status": "STOPPED"},
                }
            if requested == "start":
                return {"status": "OK", "ok": True}
            assert requested == "open"
            return {"status": "OK", "ok": True, "opened": True}

    output = io.StringIO()
    result = cli.main(
        ["launch", "webui"],
        service_controller=Controller(),
        output=output,
    )

    assert result == 0
    assert calls == ["status", "start", "open"]


def test_launch_webui_intentional_same_boot_stop_requires_explicit_start_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    approvals: list[dict[str, object]] = []

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            assert requested == "status"
            return {
                "status": "OK",
                "unit": {"active": False},
                "service": {"status": "STOPPED_UNTIL_REBOOT"},
            }

    def deny(status: Mapping[str, object]) -> bool:
        approvals.append(dict(status))
        return False

    monkeypatch.setattr(cli, "_confirm_stopped_launch_start", deny)
    output = io.StringIO()
    result = cli.main(
        ["launch", "webui"],
        service_controller=Controller(),
        output=output,
    )

    payload = json.loads(output.getvalue())
    assert result == 1
    assert calls == ["status"]
    assert len(approvals) == 1
    assert approvals[0]["service"] == {"status": "STOPPED_UNTIL_REBOOT"}
    assert payload == {
        "command": "launch webui",
        "error_code": "SERVICE_START_REQUIRES_APPROVAL",
        "message": (
            "FactLane Owner Panel remains stopped until the next device restart. "
            "Start it explicitly with 'factlane start webui' if you want to reopen it earlier."
        ),
        "status": "BLOCKED",
    }


def test_launch_webui_intentional_same_boot_stop_starts_only_after_explicit_approval() -> None:
    calls: list[str] = []
    approvals: list[str] = []

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            if requested == "status":
                return {
                    "status": "OK",
                    "unit": {"active": False},
                    "service": {"status": "STOPPED_UNTIL_REBOOT"},
                }
            if requested == "start":
                return {"status": "OK", "ok": True}
            assert requested == "open"
            return {"status": "OK", "ok": True, "opened": True}

    def approve(status: Mapping[str, object]) -> bool:
        approvals.append(str(status["service"]))
        return True

    output = io.StringIO()
    result = cli.main(
        ["launch", "webui"],
        service_controller=Controller(),
        launch_start_approval=approve,
        output=output,
    )

    assert result == 0
    assert calls == ["status", "start", "open"]
    assert approvals == ["{'status': 'STOPPED_UNTIL_REBOOT'}"]


def test_launch_webui_start_failure_stops_before_open() -> None:
    calls: list[str] = []

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            if requested == "status":
                return {
                    "status": "OK",
                    "unit": {"active": False},
                    "service": {"status": "STOPPED"},
                }
            assert requested == "start"
            return {
                "status": "ERROR",
                "error_code": "SERVICE_START_FAILED",
                "message": "start failed",
            }

    output = io.StringIO()
    result = cli.main(
        ["launch", "webui"],
        service_controller=Controller(),
        output=output,
    )

    payload = json.loads(output.getvalue())
    assert result == 1
    assert calls == ["status", "start"]
    assert payload["command"] == "launch webui"
    assert payload["error_code"] == "SERVICE_START_FAILED"


def test_launch_webui_passes_through_sensitive_approved_open_success() -> None:
    calls: list[str] = []
    approved = {
        "status": "OK",
        "ok": True,
        "opened": True,
        "sensitive_approval": {
            "status": "APPROVED",
            "request_id": "approval-123",
        },
    }

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            if requested == "status":
                return {
                    "status": "OK",
                    "unit": {"active": True},
                    "service": {"status": "READY"},
                }
            assert requested == "open"
            return approved

    output = io.StringIO()
    result = cli.main(
        ["launch", "webui"],
        service_controller=Controller(),
        output=output,
    )

    assert result == 0
    assert calls == ["status", "open"]
    assert json.loads(output.getvalue()) == {
        **approved,
        "command": "launch webui",
    }


def test_launch_webui_invalid_status_fails_closed_before_start_or_open() -> None:
    calls: list[str] = []

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            assert requested == "status"
            return {"status": "OK", "unit": {"active": False}}

    output = io.StringIO()
    result = cli.main(
        ["launch", "webui"],
        service_controller=Controller(),
        output=output,
    )

    payload = json.loads(output.getvalue())
    assert result == 1
    assert calls == ["status"]
    assert payload["error_code"] == "SERVICE_STATUS_INVALID"


def test_doctor_webui_emits_machine_readable_read_only_report_without_service_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "status": "OK",
        "mode": "READ_ONLY",
        "managed_binding": {
            "state": "UNCONFIGURED",
            "db": None,
            "profile": None,
            "supported_profiles": ["nomic-768"],
        },
        "mcp": {
            "host_id": {"state": "EXPLICIT_REQUIRED", "value": None},
            "scope": {"state": "EXPLICIT_PER_REQUEST", "value": None},
        },
        "next_actions": [
            {
                "id": "CONFIGURE_OWNER_PANEL_MEMORY",
                "message": "Choose a profile explicitly.",
            }
        ],
    }
    calls: list[str] = []
    monkeypatch.setattr(cli, "build_webui_doctor_report", lambda: report)

    class Controller:
        def dispatch(self, requested: str) -> dict[str, object]:
            calls.append(requested)
            pytest.fail("doctor must not dispatch managed service actions")

    output = io.StringIO()
    result = cli.main(
        ["doctor", "webui"],
        service_controller=Controller(),
        output=output,
    )

    payload = json.loads(output.getvalue())
    assert result == 0
    assert calls == []
    assert payload["command"] == "doctor webui"
    assert payload["mode"] == "READ_ONLY"
    assert payload["mcp"]["host_id"]["state"] == "EXPLICIT_REQUIRED"
    assert payload["mcp"]["scope"]["state"] == "EXPLICIT_PER_REQUEST"
    assert payload["next_actions"]


@pytest.mark.skipif(os.name == "nt", reason="this unit test exercises the Linux registration branch")
def test_register_database_validates_local_source_and_returns_only_custody_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from factlane import owner_database, webui_app, webui_runtime

    active = tmp_path / "active.db"
    candidate = tmp_path / "candidate.db"
    active.write_bytes(b"active")
    candidate.write_bytes(b"candidate")
    state_dir = tmp_path / "state"
    calls: dict[str, object] = {}

    config = type(
        "Config",
        (),
        {
            "memory_configured": True,
            "db": str(active),
            "profile": "nomic-768",
            "state_dir": str(state_dir),
        },
    )()

    monkeypatch.setattr(webui_runtime, "default_runtime_config_path", lambda: tmp_path / "webui.json")
    monkeypatch.setattr(
        webui_runtime,
        "load_runtime_config",
        lambda path, owner_uid: config,
    )
    monkeypatch.setattr(
        webui_app,
        "_validate_linux_setup_database_path",
        lambda source, owner_uid: str(Path(source).resolve()),
    )
    monkeypatch.setattr(
        webui_app,
        "_validate_existing_windows_setup_database",
        lambda source, profile, platform_name: calls.update(
            validation=(source, profile, platform_name)
        ),
    )
    monkeypatch.setattr(
        owner_database,
        "ArtifactRegistry",
        lambda root: calls.setdefault("registry_root", Path(root)),
    )

    def create_backup(source, registry, *, metadata_extra):
        calls["backup"] = (source, registry, metadata_extra)
        return {
            "artifact_id": "registration-1",
            "sha256": "a" * 64,
            "size_bytes": 123,
            "storage_contract_version": 3,
            "embedding_profile": "nomic-768",
        }

    monkeypatch.setattr(owner_database, "create_consistent_backup", create_backup)
    output = io.StringIO()

    result = cli.main(["register", "database", str(candidate)], output=output)

    payload = json.loads(output.getvalue())
    assert result == 0
    assert payload == {
        "command": "register database",
        "status": "REGISTERED",
        "registration_id": "registration-1",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "storage_contract_version": 3,
        "embedding_profile": "nomic-768",
    }
    assert calls["validation"] == (str(candidate.resolve()), "nomic-768", "Linux")
    assert calls["backup"][0] == str(candidate.resolve())
    assert calls["backup"][2] == {"local_database_registration": True}
    assert calls["registry_root"] == calls["backup"][1]
    assert str(candidate) not in output.getvalue()
    assert str(active) not in output.getvalue()


def test_pyproject_routes_factlane_to_cli_and_preserves_other_console_scripts() -> None:
    document = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = document["project"]["scripts"]

    assert scripts["factlane"] == "factlane.cli:main"
    assert scripts["factlane-owner"] == "factlane.owner_cli:main"
    assert scripts["factlane-owner-webui"] == "factlane.owner_webui:main"
    assert scripts["factlane-migrate-storage-v3"] == "factlane.storage_migration:main"


def test_pyproject_packages_service_template_and_recursive_webui_assets() -> None:
    document = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = document["tool"]["setuptools"]["package-data"]["factlane"]

    assert "data/embedding_compatibility/*.json" in package_data
    assert "data/factlane-webui.service" in package_data
    assert "webui/**/*" in package_data
