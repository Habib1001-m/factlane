from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable, Mapping, Protocol, TextIO

from . import server
from .webui_install import (
    InstallEnvironment,
    SetupApplyResult,
    SetupConsent,
    SetupReceipt,
    SetupRequest,
    detect_install_environment,
    plan_webui_setup,
)
from .webui_platform import (
    acquire_default_setup_consent,
    build_default_platform_controller,
    build_default_setup_executor,
    build_default_setup_request,
)


_WEBUI_ACTION_HELP = (
    ("setup", "Plan and apply the managed local WebUI installation."),
    ("serve", "Run the configured managed WebUI runtime."),
    ("open", "Open the managed WebUI through the trusted local platform flow."),
    ("start", "Start the managed WebUI service."),
    ("stop", "Stop the managed WebUI service."),
    ("status", "Report the managed WebUI service status."),
    ("restart", "Restart the managed WebUI service."),
)
_WEBUI_ACTIONS = frozenset(action for action, _description in _WEBUI_ACTION_HELP)
_SERVICE_ACTIONS = frozenset({"serve", "open", "start", "stop", "status", "restart"})


@dataclass(frozen=True)
class WebUICommand:
    action: str


@dataclass(frozen=True)
class SetupConfiguration:
    request: SetupRequest
    consent: SetupConsent
    environment: InstallEnvironment | None = None


class WebUIServiceController(Protocol):
    def dispatch(self, action: str) -> Mapping[str, Any]: ...


SetupConfigProvider = Callable[[], SetupConfiguration | None]
SetupExecutor = Callable[
    [SetupReceipt, SetupConsent],
    Mapping[str, Any] | SetupApplyResult,
]


def parse_webui_command(argv: list[str]) -> WebUICommand | None:
    """Recognize only the supported exact two-token WebUI command surface."""

    if len(argv) != 2 or argv[1] != "webui" or argv[0] not in _WEBUI_ACTIONS:
        return None
    return WebUICommand(action=argv[0])


def main(
    argv: list[str] | None = None,
    *,
    setup_config_provider: SetupConfigProvider | None = None,
    setup_executor: SetupExecutor | None = None,
    service_controller: WebUIServiceController | None = None,
    output: TextIO | None = None,
) -> Any:
    """Dispatch the bounded WebUI CLI surface and preserve every legacy MCP argv."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    stream = sys.stdout if output is None else output
    if raw_argv == ["--help"]:
        _write_webui_discovery(stream)
        with redirect_stdout(stream):
            return server.main(raw_argv)

    help_action = _webui_help_action(raw_argv)
    if help_action is not None:
        _write_webui_command_help(stream, help_action)
        return 0

    command = parse_webui_command(raw_argv)
    if command is None:
        return server.main(raw_argv)

    if command.action == "setup":
        return _dispatch_setup(
            setup_config_provider=setup_config_provider,
            setup_executor=setup_executor,
            output=stream,
        )
    return _dispatch_service(
        command.action,
        service_controller=service_controller,
        output=stream,
    )


def _webui_help_action(argv: list[str]) -> str | None:
    if (
        len(argv) == 3
        and argv[1] == "webui"
        and argv[2] == "--help"
        and argv[0] in _WEBUI_ACTIONS
    ):
        return argv[0]
    return None


def _write_webui_discovery(output: TextIO) -> None:
    output.write("Managed WebUI commands:\n")
    for action, description in _WEBUI_ACTION_HELP:
        output.write(f"  factlane {action} webui  {description}\n")
    output.write(
        "  Run `factlane <action> webui --help` for managed command help.\n\n"
        "Legacy stdio MCP server options:\n"
    )
    output.flush()


def _write_webui_command_help(output: TextIO, action: str) -> None:
    descriptions = dict(_WEBUI_ACTION_HELP)
    output.write(f"usage: factlane {action} webui\n\n")
    output.write(f"Managed WebUI command: {descriptions[action]}\n")
    output.write("Execution form accepts no additional command-line options.\n")
    output.flush()


def _dispatch_setup(
    *,
    setup_config_provider: SetupConfigProvider | None,
    setup_executor: SetupExecutor | None,
    output: TextIO,
) -> int:
    if setup_config_provider is None:
        setup_config_provider = _default_setup_configuration

    try:
        configuration = setup_config_provider()
    except Exception:
        return _emit(
            output,
            {
                "command": "setup webui",
                "error_code": "CONFIG_UNAVAILABLE",
                "message": "FactLane WebUI setup configuration could not be loaded",
                "status": "BLOCKED",
            },
            exit_code=1,
        )
    if configuration is None:
        return _emit(
            output,
            {
                "command": "setup webui",
                "error_code": "NOT_CONFIGURED",
                "message": "FactLane WebUI setup configuration is not available",
                "status": "BLOCKED",
            },
            exit_code=1,
        )

    environment = configuration.environment or detect_install_environment()
    try:
        receipt = plan_webui_setup(
            configuration.request,
            environment,
            configuration.consent,
        )
    except (TypeError, ValueError):
        return _emit(
            output,
            {
                "command": "setup webui",
                "error_code": "INVALID_CONFIGURATION",
                "message": "FactLane WebUI setup configuration is invalid",
                "status": "BLOCKED",
            },
            exit_code=1,
        )

    payload = asdict(receipt)
    payload["command"] = "setup webui"
    payload["apply_available"] = setup_executor is not None
    if receipt.status != "READY_TO_APPLY" or receipt.plan is None:
        return _emit(output, payload, exit_code=1)
    if setup_executor is None:
        try:
            setup_executor = build_default_setup_executor(
                configuration.request,
                environment,
            )
        except Exception:
            payload["apply"] = {
                "status": "FAILED",
                "detail": "default WebUI setup executor could not be composed safely",
            }
            return _emit(output, payload, exit_code=1)
    payload["apply_available"] = True

    try:
        raw_apply_result = setup_executor(receipt, configuration.consent)
        if is_dataclass(raw_apply_result):
            apply_result = asdict(raw_apply_result)
        elif isinstance(raw_apply_result, Mapping):
            apply_result = dict(raw_apply_result)
        else:
            raise TypeError("setup executor result must be a mapping or dataclass")
    except Exception:
        payload["apply"] = {
            "error_code": "APPLY_FAILED",
            "message": "Injected WebUI setup executor failed",
            "status": "BLOCKED",
        }
        return _emit(output, payload, exit_code=1)
    payload["apply"] = apply_result
    return _emit(output, payload, exit_code=0 if _setup_apply_ok(apply_result) else 1)


def _dispatch_service(
    action: str,
    *,
    service_controller: WebUIServiceController | None,
    output: TextIO,
) -> int:
    if action not in _SERVICE_ACTIONS:
        raise ValueError("unsupported WebUI service action")
    if service_controller is None:
        try:
            service_controller = build_default_platform_controller()
        except Exception:
            return _emit(
                output,
                {
                    "command": f"{action} webui",
                    "error_code": "PLATFORM_UNAVAILABLE",
                    "message": "FactLane WebUI platform controller could not be initialized",
                    "status": "ERROR",
                },
                exit_code=1,
            )

    try:
        result = dict(service_controller.dispatch(action))
    except Exception:
        return _emit(
            output,
            {
                "command": f"{action} webui",
                "error_code": "SERVICE_CONTROL_FAILED",
                "message": "FactLane WebUI service control failed safely",
                "status": "ERROR",
            },
            exit_code=1,
        )
    result["command"] = f"{action} webui"
    return _emit(output, result, exit_code=0 if _result_ok(result) else 1)


def _result_ok(result: Mapping[str, Any]) -> bool:
    if "ok" in result:
        return result["ok"] is True
    return result.get("status") not in {
        "BLOCKED",
        "ERROR",
        "FAILED",
        "NOT_CONFIGURED",
        "UNSUPPORTED",
    }


def _setup_apply_ok(result: Mapping[str, Any]) -> bool:
    if "ok" in result:
        return result["ok"] is True
    status = result.get("status")
    return getattr(status, "value", status) == "APPLIED"


def _default_setup_configuration() -> SetupConfiguration:
    environment = detect_install_environment()
    request = build_default_setup_request()
    return SetupConfiguration(
        request=request,
        consent=acquire_default_setup_consent(request, environment),
        environment=environment,
    )


def _emit(output: TextIO, payload: Mapping[str, Any], *, exit_code: int) -> int:
    output.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    output.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
