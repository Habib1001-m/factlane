from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path
from typing import Any

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
                family="chromium",
                executable="/usr/bin/google-chrome",
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
        ["setup", "other"],
        ["serve", "webui", "--config", "/tmp/webui.json"],
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
    ["setup", "serve", "open", "start", "stop", "status", "restart"],
)
def test_only_exact_supported_webui_pairs_are_recognized(action: str) -> None:
    parsed = cli.parse_webui_command([action, "webui"])

    assert parsed is not None
    assert parsed.action == action
    assert cli.parse_webui_command([action, "webui", "extra"]) is None
    assert cli.parse_webui_command([action.upper(), "webui"]) is None
    assert cli.parse_webui_command([action, "WEBUI"]) is None


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
    assert payload["plan"]["origin"] == "https://factlane-abc123.test:8768"
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

    def build_executor(request: object, environment: object):
        executor_builds.append((request, environment))

        def executor(receipt: object, consent: object) -> SetupApplyResult:
            executor_calls.append((receipt, consent))
            return SetupApplyResult(
                status=InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER,
                endpoint_state=EndpointState.PROPOSED,
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
    assert applied[0][0].plan.origin == "https://factlane-abc123.test:8768"
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
