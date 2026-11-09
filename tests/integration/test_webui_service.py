from __future__ import annotations

import json
import stat
from pathlib import Path
import sys

import pytest

if sys.platform != "linux":
    pytest.skip(
        "managed Owner WebUI service-state qualification is Linux-only",
        allow_module_level=True,
    )

from factlane.webui_service import (
    EndpointUnavailable,
    ServiceStateError,
    ServiceStatus,
    WebUIServiceState,
)


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Boot:
    def __init__(self, value: str = "boot-a") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


class _Controller:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def __call__(self, action: str) -> None:
        self.actions.append(action)


def _service(
    tmp_path: Path,
    *,
    clock: _Clock | None = None,
    boot: _Boot | None = None,
    controller: _Controller | None = None,
    listener=None,
) -> WebUIServiceState:
    return WebUIServiceState(
        tmp_path / "owner-private" / "webui-service.json",
        controller=controller or _Controller(),
        clock=clock or _Clock(),
        boot_id=boot or _Boot(),
        listener=listener,
    )


def test_stop_marker_is_owner_private_persistent_and_expires_only_on_new_boot(tmp_path: Path) -> None:
    clock = _Clock(10.0)
    boot = _Boot("boot-a")
    controller = _Controller()
    service = _service(tmp_path, clock=clock, boot=boot, controller=controller)

    stopped = service.request_stop("stop-1")
    assert stopped.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert stopped.stopped_boot_id == "boot-a"
    assert controller.actions == ["stop"]

    state_path = tmp_path / "owner-private" / "webui-service.json"
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["stopped_boot_id"] == "boot-a"

    same_boot = _service(tmp_path, clock=clock, boot=boot, controller=_Controller())
    assert same_boot.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT

    boot.value = "boot-b"
    next_boot = _service(tmp_path, clock=clock, boot=boot, controller=_Controller())
    assert next_boot.snapshot().status is ServiceStatus.STARTING
    assert next_boot.snapshot().stopped_boot_id is None


def test_first_endpoint_is_persisted_only_after_successful_bind_and_never_silently_rebound(tmp_path: Path) -> None:
    attempts: list[int] = []

    def bind_first_install(port: int) -> bool:
        attempts.append(port)
        return port == 8767

    service = _service(tmp_path, listener=bind_first_install)
    endpoint = service.ensure_endpoint("factlane-installation.test")

    assert endpoint == "https://factlane-installation.test:8767"
    assert attempts == [8765, 8766, 8767]
    assert service.snapshot().saved_endpoint == endpoint

    later_attempts: list[int] = []

    def saved_port_is_busy(port: int) -> bool:
        later_attempts.append(port)
        return False

    later = _service(tmp_path, listener=saved_port_is_busy)
    with pytest.raises(EndpointUnavailable, match="saved endpoint"):
        later.ensure_endpoint("factlane-installation.test")

    assert later_attempts == [8767]
    assert later.snapshot().saved_endpoint == endpoint


def test_claim_endpoint_persists_only_exact_canonical_ready_origin_and_is_idempotent(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    endpoint = "https://factlane-installation.test:8767"

    first = service.claim_endpoint(endpoint)
    second = service.claim_endpoint(endpoint)

    assert first.saved_endpoint == endpoint
    assert second.saved_endpoint == endpoint
    persisted = json.loads(
        (tmp_path / "owner-private" / "webui-service.json").read_text(encoding="utf-8")
    )
    assert persisted["saved_endpoint"] == endpoint


@pytest.mark.parametrize(
    "origin",
    [
        "http://factlane-installation.test:8767",
        "https://localhost:8767",
        "https://factlane-installation.test:8764",
        "https://factlane-installation.test:8775",
        "https://factlane-installation.test:8767/path",
        "https://user@factlane-installation.test:8767",
        "https://FACTLANE-installation.test:8767",
    ],
)
def test_claim_endpoint_rejects_noncanonical_origin(tmp_path: Path, origin: str) -> None:
    service = _service(tmp_path)

    with pytest.raises(EndpointUnavailable, match="canonical"):
        service.claim_endpoint(origin)

    assert service.snapshot().saved_endpoint is None


def test_claim_endpoint_rejects_different_saved_endpoint_with_explicit_recovery(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.claim_endpoint("https://factlane-installation.test:8767")

    with pytest.raises(EndpointUnavailable, match="explicit recovery"):
        service.claim_endpoint("https://factlane-installation.test:8768")

    assert service.snapshot().saved_endpoint == "https://factlane-installation.test:8767"


def test_start_stop_and_restart_intents_are_idempotent(tmp_path: Path) -> None:
    controller = _Controller()
    service = _service(tmp_path, controller=controller)

    first_stop = service.request_stop("stop-1")
    duplicate_stop = service.request_stop("stop-1")
    repeated_stop = service.request_stop("stop-2")
    assert duplicate_stop == first_stop
    assert repeated_stop == first_stop
    assert controller.actions == ["stop"]

    service.request_start("start-1")
    service.request_start("start-1")
    service.request_start("start-2")
    assert service.snapshot().status is ServiceStatus.STARTING
    assert controller.actions == ["stop", "start"]

    service.request_restart("restart-1")
    duplicate_restart = service.request_restart("restart-1")
    assert duplicate_restart == service.snapshot()
    assert controller.actions == ["stop", "start", "restart"]


def test_maintenance_restores_initial_running_state_unless_a_newer_stop_wins(tmp_path: Path) -> None:
    controller = _Controller()
    service = _service(tmp_path, controller=controller)
    service.mark_ready()

    service.begin_maintenance("maintenance-1")
    assert service.snapshot().status is ServiceStatus.MAINTENANCE
    service.finish_maintenance("maintenance-1")
    assert service.snapshot().status is ServiceStatus.STARTING
    assert controller.actions == ["start"]

    service.mark_ready()
    service.begin_maintenance("maintenance-2")
    service.request_stop("stop-during-maintenance")
    assert service.snapshot().status is ServiceStatus.MAINTENANCE
    service.finish_maintenance("maintenance-2")
    assert service.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert controller.actions == ["start", "stop"]


def test_governance_revision_is_monotonic_across_maintenance_and_recovery_aba(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    initial = service.snapshot().governance_revision

    maintenance = service.begin_maintenance("governance-maintenance")
    assert maintenance.governance_revision == initial + 1
    finished = service.finish_maintenance("governance-maintenance")
    assert finished.status is ServiceStatus.STARTING
    assert finished.governance_revision == initial + 2

    recovery = service.mark_recovery_required()
    assert recovery.governance_revision == initial + 3
    cleared = service.clear_recovery_required()
    assert cleared.status is ServiceStatus.STARTING
    assert cleared.governance_revision == initial + 4

    stopped = service.request_stop("governance-stop")
    assert stopped.governance_revision == initial + 5
    started = service.request_start("governance-start")
    assert started.governance_revision == initial + 6


def test_legacy_service_state_without_governance_revision_loads_as_zero(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.mark_ready()
    state_path = tmp_path / "owner-private" / "webui-service.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload.pop("governance_revision")
    state_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    reloaded = _service(tmp_path)

    assert reloaded.snapshot().governance_revision == 0


def test_stale_runtime_instance_cannot_overwrite_newer_owner_stop(tmp_path: Path) -> None:
    state_path = tmp_path / "owner-private" / "webui-service.json"
    runtime = WebUIServiceState(state_path, boot_id=lambda: "boot-shared")
    owner = WebUIServiceState(state_path, boot_id=lambda: "boot-shared")

    stopped = owner.request_stop("owner-stop")
    assert stopped.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    with pytest.raises(ServiceStateError, match="stopped-until-reboot"):
        runtime.mark_ready()

    persisted = WebUIServiceState(state_path, boot_id=lambda: "boot-shared").snapshot()
    assert persisted.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert persisted.intent_revision == 1
    assert persisted.governance_revision == 1


def test_stale_owner_instance_preserves_newer_runtime_endpoint_claim(tmp_path: Path) -> None:
    state_path = tmp_path / "owner-private" / "webui-service.json"
    stale_owner = WebUIServiceState(state_path, boot_id=lambda: "boot-shared")
    runtime = WebUIServiceState(state_path, boot_id=lambda: "boot-shared")
    endpoint = "https://factlane-demo.test:8765"

    runtime.claim_endpoint(endpoint)
    runtime.mark_ready()
    started = stale_owner.request_start("owner-start")

    assert started.saved_endpoint == endpoint
    assert started.intent_revision == 1
    persisted = WebUIServiceState(state_path, boot_id=lambda: "boot-shared").snapshot()
    assert persisted.saved_endpoint == endpoint
    assert persisted.intent_revision == 1


def test_runtime_failure_cannot_demote_newer_owner_stop(tmp_path: Path) -> None:
    state_path = tmp_path / "owner-private" / "webui-service.json"
    runtime = WebUIServiceState(state_path, boot_id=lambda: "boot-shared")
    owner = WebUIServiceState(state_path, boot_id=lambda: "boot-shared")

    owner.request_stop("owner-stop")
    failed = runtime.mark_failed()

    assert failed.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    persisted = WebUIServiceState(state_path, boot_id=lambda: "boot-shared").snapshot()
    assert persisted.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert persisted.intent_revision == 1
    assert persisted.governance_revision == 1


def test_runtime_status_writers_cannot_demote_maintenance_or_recovery(tmp_path: Path) -> None:
    state_path = tmp_path / "owner-private" / "webui-service.json"
    runtime = WebUIServiceState(state_path, boot_id=lambda: "boot-shared")
    owner = WebUIServiceState(state_path, boot_id=lambda: "boot-shared")

    owner.begin_maintenance("maintenance-1")
    assert runtime.mark_starting().status is ServiceStatus.MAINTENANCE
    assert runtime.mark_failed().status is ServiceStatus.MAINTENANCE
    with pytest.raises(ServiceStateError, match="maintenance-owned"):
        runtime.mark_ready()
    maintained = owner.snapshot()
    assert maintained.status is ServiceStatus.MAINTENANCE
    assert maintained.maintenance_operation_id == "maintenance-1"

    owner.mark_recovery_required()
    assert runtime.mark_starting().status is ServiceStatus.RECOVERY_REQUIRED
    assert runtime.mark_failed().status is ServiceStatus.RECOVERY_REQUIRED
    with pytest.raises(ServiceStateError, match="recovery-required"):
        runtime.mark_ready()
    recovered = owner.snapshot()
    assert recovered.status is ServiceStatus.RECOVERY_REQUIRED
    assert recovered.maintenance_operation_id is None


def test_explicit_failed_recovery_finalization_preserves_newer_owner_stop(tmp_path: Path) -> None:
    state_path = tmp_path / "owner-private" / "webui-service.json"
    service = WebUIServiceState(state_path, boot_id=lambda: "boot-shared")
    service.mark_failed()
    service.begin_maintenance("maintenance-1")
    service.mark_recovery_required()

    restored = service.restore_failed_after_recovery()
    assert restored.status is ServiceStatus.FAILED
    assert restored.governance_revision == 3

    service.mark_recovery_required()
    service.request_stop("owner-stop-during-recovery")
    stopped = service.restore_failed_after_recovery()
    assert stopped.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert stopped.stopped_boot_id == "boot-shared"
    assert stopped.intent_revision == 1


@pytest.mark.parametrize("protected", ["stop", "maintenance"])
def test_protected_service_state_refuses_new_endpoint_claims(
    tmp_path: Path,
    protected: str,
) -> None:
    state_path = tmp_path / "owner-private" / "webui-service.json"
    service = WebUIServiceState(
        state_path,
        boot_id=lambda: "boot-protected",
        listener=lambda _port: True,
    )
    if protected == "stop":
        service.request_stop("owner-stop")
    elif protected == "maintenance":
        service.begin_maintenance("maintenance-1")

    with pytest.raises(EndpointUnavailable):
        service.claim_endpoint("https://factlane-demo.test:8765")
    with pytest.raises(EndpointUnavailable):
        service.ensure_endpoint("factlane-demo.test")

    persisted = service.snapshot()
    assert persisted.saved_endpoint is None


def test_maintenance_preserves_an_initial_stopped_state(tmp_path: Path) -> None:
    controller = _Controller()
    service = _service(tmp_path, controller=controller)
    service.request_stop("stop-before-maintenance")
    service.begin_maintenance("maintenance-stopped")

    service.finish_maintenance("maintenance-stopped")

    assert service.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert controller.actions == ["stop"]


def test_crash_incident_allows_only_three_retries_five_seconds_apart(tmp_path: Path) -> None:
    clock = _Clock(100.0)
    service = _service(tmp_path, clock=clock)

    for attempt in range(1, 4):
        retry = service.record_crash()
        assert retry is not None
        assert retry.attempt == attempt
        assert retry.retry_at == pytest.approx(clock.value + 5.0)
        assert service.snapshot().status is ServiceStatus.STARTING
        clock.advance(5.0)
        service.mark_ready()
        clock.advance(1.0)

    exhausted = service.record_crash()
    assert exhausted is None
    assert service.snapshot().status is ServiceStatus.FAILED
    assert service.snapshot().crash_attempts == 3


def test_explicit_start_from_exhausted_failed_state_starts_a_new_crash_incident(tmp_path: Path) -> None:
    controller = _Controller()
    service = _service(tmp_path, controller=controller)
    for _ in range(3):
        assert service.record_crash() is not None
        service.mark_ready()
    assert service.record_crash() is None
    assert service.snapshot().status is ServiceStatus.FAILED
    assert service.snapshot().crash_attempts == 3

    started = service.request_start("recover-failed-start")

    assert started.status is ServiceStatus.STARTING
    assert started.crash_attempts == 0
    assert started.next_retry_at is None
    assert controller.actions == ["start"]
    retry = service.record_crash()
    assert retry is not None
    assert retry.attempt == 1


def test_explicit_restart_from_exhausted_failed_state_starts_a_new_crash_incident(tmp_path: Path) -> None:
    controller = _Controller()
    service = _service(tmp_path, controller=controller)
    for _ in range(3):
        assert service.record_crash() is not None
        service.mark_ready()
    assert service.record_crash() is None
    assert service.snapshot().status is ServiceStatus.FAILED

    restarted = service.request_restart("recover-failed-restart")

    assert restarted.status is ServiceStatus.STARTING
    assert restarted.crash_attempts == 0
    assert restarted.next_retry_at is None
    assert controller.actions == ["restart"]
    retry = service.record_crash()
    assert retry is not None
    assert retry.attempt == 1


def test_explicit_start_resets_an_active_incident_without_duplicate_controller_start(tmp_path: Path) -> None:
    controller = _Controller()
    service = _service(tmp_path, controller=controller)
    retry = service.record_crash()
    assert retry is not None
    assert service.snapshot().status is ServiceStatus.STARTING
    assert service.snapshot().crash_attempts == 1
    assert service.snapshot().next_retry_at is not None

    started = service.request_start("explicit-start-active-incident")

    assert started.status is ServiceStatus.STARTING
    assert started.crash_attempts == 0
    assert started.next_retry_at is None
    assert controller.actions == []


def test_new_boot_recovers_exhausted_failed_state_and_starts_a_new_crash_incident(tmp_path: Path) -> None:
    boot = _Boot("boot-a")
    service = _service(tmp_path, boot=boot)
    for _ in range(3):
        assert service.record_crash() is not None
        service.mark_ready()
    assert service.record_crash() is None
    assert service.snapshot().status is ServiceStatus.FAILED
    assert service.snapshot().crash_attempts == 3

    boot.value = "boot-b"
    next_boot = _service(tmp_path, boot=boot)

    assert next_boot.snapshot().status is ServiceStatus.STARTING
    assert next_boot.snapshot().crash_attempts == 0
    assert next_boot.snapshot().next_retry_at is None
    retry = next_boot.record_crash()
    assert retry is not None
    assert retry.attempt == 1


def test_crash_incident_resets_only_after_ten_continuous_ready_minutes(tmp_path: Path) -> None:
    clock = _Clock(20.0)
    service = _service(tmp_path, clock=clock)
    assert service.record_crash() is not None
    service.mark_ready()

    clock.advance(599.0)
    service.observe_ready()
    assert service.snapshot().crash_attempts == 1

    clock.advance(1.0)
    service.observe_ready()
    assert service.snapshot().crash_attempts == 0

    assert service.record_crash() is not None
    assert service.snapshot().crash_attempts == 1
    service.mark_starting()
    service.mark_ready()
    clock.advance(599.0)
    service.observe_ready()
    assert service.snapshot().crash_attempts == 1


def test_stable_ready_period_starts_a_new_crash_incident_without_an_observation_poll(tmp_path: Path) -> None:
    clock = _Clock(40.0)
    service = _service(tmp_path, clock=clock)
    assert service.record_crash() is not None
    service.mark_ready()
    clock.advance(600.0)

    retry = service.record_crash()

    assert retry is not None
    assert retry.attempt == 1
    assert service.snapshot().crash_attempts == 1


def test_explicit_stop_suppresses_automatic_crash_retry(tmp_path: Path) -> None:
    clock = _Clock(50.0)
    service = _service(tmp_path, clock=clock)
    service.request_stop("stop-before-crash")

    retry = service.record_crash()

    assert retry is None
    assert service.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert service.snapshot().crash_attempts == 0
    assert service.snapshot().next_retry_at is None


def test_new_boot_discards_monotonic_retry_deadline_without_resetting_incident_count(tmp_path: Path) -> None:
    clock = _Clock(75.0)
    boot = _Boot("boot-a")
    service = _service(tmp_path, clock=clock, boot=boot)
    assert service.record_crash() is not None
    assert service.snapshot().next_retry_at == pytest.approx(80.0)

    boot.value = "boot-b"
    next_boot = _service(tmp_path, clock=clock, boot=boot)

    assert next_boot.snapshot().status is ServiceStatus.STARTING
    assert next_boot.snapshot().crash_attempts == 1
    assert next_boot.snapshot().next_retry_at is None


def test_recovery_required_outlives_start_intent_and_clears_to_requested_state(tmp_path: Path) -> None:
    controller = _Controller()
    service = _service(tmp_path, controller=controller)
    service.mark_recovery_required()

    service.request_start("start-during-recovery")
    assert service.snapshot().status is ServiceStatus.RECOVERY_REQUIRED
    assert controller.actions == []

    service.clear_recovery_required()
    assert service.snapshot().status is ServiceStatus.STARTING
    assert controller.actions == ["start"]


def test_recovery_required_blocks_explicit_start_from_resetting_crash_incident(tmp_path: Path) -> None:
    controller = _Controller()
    service = _service(tmp_path, controller=controller)
    assert service.record_crash() is not None
    service.mark_recovery_required()
    assert service.snapshot().crash_attempts == 1

    blocked = service.request_start("blocked-start")

    assert blocked.status is ServiceStatus.RECOVERY_REQUIRED
    assert blocked.crash_attempts == 1
    assert blocked.next_retry_at is None
    assert controller.actions == []
