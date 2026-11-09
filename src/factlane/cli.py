from __future__ import annotations

import json
import os
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
    build_webui_doctor_report,
)


_WEBUI_ACTION_HELP = (
    ("setup", "Plan and apply the managed local WebUI installation."),
    ("doctor", "Report read-only managed WebUI onboarding facts and next actions."),
    ("launch", "Start the managed WebUI when needed, then open it."),
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
LaunchStartApproval = Callable[[Mapping[str, Any]], bool]


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
    launch_start_approval: LaunchStartApproval | None = None,
    output: TextIO | None = None,
) -> Any:
    """Dispatch the bounded WebUI CLI surface and preserve every legacy MCP argv."""

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    stream = sys.stdout if output is None else output
    if len(raw_argv) == 3 and raw_argv[:2] == ["register", "database"]:
        return _dispatch_database_registration(raw_argv[2], output=stream)
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
    if command.action == "doctor":
        return _dispatch_doctor(output=stream)
    if command.action == "launch":
        return _dispatch_launch(
            service_controller=service_controller,
            start_approval=launch_start_approval,
            output=stream,
        )
    return _dispatch_service(
        command.action,
        service_controller=service_controller,
        output=stream,
    )


def _dispatch_database_registration(source: str, *, output: TextIO) -> int:
    """Register one compatible local database without exposing its path to the browser."""

    try:
        import hashlib
        from pathlib import Path

        from .owner_database import ArtifactRegistry, create_consistent_backup
        from .webui_app import _validate_existing_windows_setup_database, _validate_linux_setup_database_path
        from .webui_runtime import default_runtime_config_path, load_runtime_config

        if os.name == "nt":
            from .webui_windows import load_windows_managed_config, windows_owner_paths
            from .windows_security import current_user_sid

            sid = current_user_sid()
            paths = windows_owner_paths(owner_sid=sid)
            config = load_windows_managed_config(paths.service_config_path, owner_sid=sid)
            if config is None or config.db is None or config.profile is None:
                raise ValueError("managed database binding is unavailable")
            from .webui_windows import validate_windows_database_binding_path

            candidate = validate_windows_database_binding_path(source, owner_sid=sid)
            platform_name = "Windows"
        else:
            uid = os.getuid()
            config = load_runtime_config(default_runtime_config_path(), owner_uid=uid)
            if not config.memory_configured or config.db is None or config.profile is None:
                raise ValueError("managed database binding is unavailable")
            candidate = _validate_linux_setup_database_path(source, owner_uid=uid)
            platform_name = "Linux"
        _validate_existing_windows_setup_database(candidate, config.profile, platform_name=platform_name)
        active = os.path.realpath(os.path.abspath(config.db))
        if os.path.realpath(os.path.abspath(candidate)) == active:
            raise ValueError("active database is already selected")
        digest = hashlib.sha256(active.encode("utf-8")).hexdigest()[:32]
        artifacts_root = Path(config.state_dir).absolute() / "database" / digest / "artifacts"
        registry = ArtifactRegistry(artifacts_root)
        receipt = create_consistent_backup(
            candidate,
            registry,
            metadata_extra={"local_database_registration": True},
        )
    except Exception as exc:
        return _emit(
            output,
            {
                "command": "register database",
                "error_code": getattr(exc, "code", "DATABASE_REGISTRATION_FAILED"),
                "message": "Local database registration failed validation or custody creation",
                "status": "BLOCKED",
            },
            exit_code=1,
        )
    return _emit(
        output,
        {
            "command": "register database",
            "status": "REGISTERED",
            "registration_id": receipt["artifact_id"],
            "sha256": receipt["sha256"],
            "size_bytes": receipt["size_bytes"],
            "storage_contract_version": receipt.get("storage_contract_version"),
            "embedding_profile": receipt.get("embedding_profile"),
        },
        exit_code=0,
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
        "Local database custody:\n"
        "  factlane register database <path>  Validate and register a local database for Owner switching.\n\n"
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


def _dispatch_doctor(*, output: TextIO) -> int:
    try:
        result = dict(build_webui_doctor_report())
    except Exception:
        return _emit(
            output,
            {
                "command": "doctor webui",
                "error_code": "DOCTOR_UNAVAILABLE",
                "message": "FactLane WebUI onboarding facts could not be inspected safely",
                "status": "ERROR",
            },
            exit_code=1,
        )
    result["command"] = "doctor webui"
    return _emit(output, result, exit_code=0 if _result_ok(result) else 1)


def _dispatch_launch(
    *,
    service_controller: WebUIServiceController | None,
    start_approval: LaunchStartApproval | None,
    output: TextIO,
) -> int:
    if service_controller is None:
        try:
            service_controller = build_default_platform_controller()
        except Exception:
            return _emit(
                output,
                {
                    "command": "launch webui",
                    "error_code": "PLATFORM_UNAVAILABLE",
                    "message": "FactLane WebUI platform controller could not be initialized",
                    "status": "ERROR",
                },
                exit_code=1,
            )

    try:
        status_result = dict(service_controller.dispatch("status"))
    except Exception:
        return _emit(
            output,
            {
                "command": "launch webui",
                "error_code": "SERVICE_CONTROL_FAILED",
                "message": "FactLane WebUI service control failed safely",
                "status": "ERROR",
            },
            exit_code=1,
        )
    if not _result_ok(status_result):
        status_result["command"] = "launch webui"
        return _emit(output, status_result, exit_code=1)

    try:
        ready = _launch_status_ready(status_result)
        service_status = _launch_service_status(status_result)
    except (TypeError, ValueError):
        return _emit(
            output,
            {
                "command": "launch webui",
                "error_code": "SERVICE_STATUS_INVALID",
                "message": "FactLane WebUI service status could not be interpreted safely",
                "status": "ERROR",
            },
            exit_code=1,
        )

    if not ready and service_status == "STOPPED_UNTIL_REBOOT":
        approval = start_approval or _confirm_stopped_launch_start
        try:
            approved = approval(status_result)
        except Exception:
            approved = False
        if not approved:
            return _emit(
                output,
                {
                    "command": "launch webui",
                    "error_code": "SERVICE_START_REQUIRES_APPROVAL",
                    "message": (
                        "FactLane Owner Panel remains stopped until the next device restart. "
                        "Start it explicitly with 'factlane start webui' if you want to reopen it earlier."
                    ),
                    "status": "BLOCKED",
                },
                exit_code=1,
            )

    if not ready:
        try:
            start_result = dict(service_controller.dispatch("start"))
        except Exception:
            return _emit(
                output,
                {
                    "command": "launch webui",
                    "error_code": "SERVICE_CONTROL_FAILED",
                    "message": "FactLane WebUI service control failed safely",
                    "status": "ERROR",
                },
                exit_code=1,
            )
        if not _result_ok(start_result):
            start_result["command"] = "launch webui"
            return _emit(output, start_result, exit_code=1)

    try:
        open_result = dict(service_controller.dispatch("open"))
    except Exception:
        return _emit(
            output,
            {
                "command": "launch webui",
                "error_code": "SERVICE_CONTROL_FAILED",
                "message": "FactLane WebUI service control failed safely",
                "status": "ERROR",
            },
            exit_code=1,
        )
    open_result["command"] = "launch webui"
    return _emit(output, open_result, exit_code=0 if _result_ok(open_result) else 1)


def _launch_status_ready(result: Mapping[str, Any]) -> bool:
    unit = result.get("unit")
    if not isinstance(unit, Mapping):
        raise TypeError("launch status requires unit and service mappings")
    active = unit.get("active")
    service_status = _launch_service_status(result)
    if not isinstance(active, bool):
        raise ValueError("launch status requires active and service status values")
    return active and service_status == "READY"


def _launch_service_status(result: Mapping[str, Any]) -> str:
    service = result.get("service")
    if not isinstance(service, Mapping):
        raise TypeError("launch status requires unit and service mappings")
    service_status = service.get("status")
    if not isinstance(service_status, str) or not service_status:
        raise ValueError("launch status requires active and service status values")
    return service_status


def _confirm_stopped_launch_start(_status_result: Mapping[str, Any]) -> bool:
    message = (
        "The FactLane Owner Panel was intentionally stopped until the next device restart.\n\n"
        "Start the dashboard now? Agent memory remains available while the dashboard is stopped."
    )
    if os.name == "nt":
        from .webui_windows import windows_native_confirm

        return bool(windows_native_confirm(message))

    from .owner_auth import NativeConfirmationRequest
    from .owner_native_control import ZenityNativeConfirmation

    caller_uid = os.getuid() if hasattr(os, "getuid") else 0
    return ZenityNativeConfirmation().confirm(
        NativeConfirmationRequest(
            kind="SERVICE_START",
            verification_phrase="START WEBUI NOW",
            auth_epoch=0,
            caller_uid=caller_uid,
            preview=message,
        )
    )


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
