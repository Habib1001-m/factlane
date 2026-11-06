from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

if os.name == "nt":
    pytest.skip(
        "maintenance coordinator qualification requires POSIX fcntl/flock",
        allow_module_level=True,
    )

import factlane.maintenance_coordinator as maintenance_coordinator_module
from factlane.maintenance_coordinator import (
    MaintenanceBarrierStore,
    MaintenanceCoordinator,
    MaintenanceCoordinatorError,
    PreparedMaintenance,
    ResumeReport,
)
from factlane.runtime_control import PROTOCOL_VERSION, RuntimeControlRequest, RuntimeIdentity, RuntimeRegistry


def _identity(tmp_path: Path, *, marker: str, state: str = "OPEN", generation: int = 1) -> RuntimeIdentity:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    db_path = tmp_path / "memory.db"
    db_path.touch(exist_ok=True)
    db_stat = db_path.stat()
    return RuntimeIdentity(
        protocol_version=PROTOCOL_VERSION,
        instance_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, marker)),
        owner_uid=os.getuid(),
        pid=os.getpid(),
        process_start_ticks=202,
        boot_id="boot-a",
        database_path=str(db_path.resolve()),
        device=db_stat.st_dev,
        inode=db_stat.st_ino,
        generation=generation,
        control_socket_path=str(runtime_dir / f"{marker}.sock"),
        lifecycle_state=state,
    )


def _registry(tmp_path: Path, identities: list[RuntimeIdentity]) -> RuntimeRegistry:
    registry = RuntimeRegistry(
        tmp_path / "runtime",
        owner_uid=os.getuid(),
        boot_id_provider=lambda: "boot-a",
        process_start_provider=lambda pid: 202 if pid == os.getpid() else None,
    )
    for identity in identities:
        registry.register(identity)
    return registry


def _response(
    request: RuntimeControlRequest,
    *,
    state: str,
    generation: int,
    status_code: str = "OK",
    lease_open: bool = False,
) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request.request_id,
        "operation_id": request.operation_id,
        "instance_id": request.instance_id,
        "state": state,
        "actual_generation": generation,
        "status_code": status_code,
        "lease_open": lease_open,
    }


def test_barrier_state_is_durable_generation_bound_and_single_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    db_path.touch()
    store = MaintenanceBarrierStore(tmp_path / "barrier", str(db_path))
    initial = store.initialize(generation=7)
    assert initial.state == "RUNNING"
    assert initial.generation == 7

    operation_1 = str(uuid.uuid4())
    prepared = store.begin_prepare(operation_1, expected_generation=7)
    assert prepared.state == "PREPARING"
    assert prepared.operation_id == operation_1
    assert prepared.intent_revision == 1

    reloaded = MaintenanceBarrierStore(tmp_path / "barrier", str(db_path)).read()
    assert reloaded == prepared

    with pytest.raises(MaintenanceCoordinatorError) as exc_info:
        store.begin_prepare(str(uuid.uuid4()), expected_generation=7)
    assert exc_info.value.code == "MAINTENANCE_BUSY"

    aborted = store.publish_abort(operation_1)
    assert aborted.state == "ABORTING"
    running = store.publish_running(operation_1, generation=7)
    assert running.state == "RUNNING"
    assert running.operation_id == operation_1
    assert running.generation == 7


def test_barrier_guard_contention_fails_busy_instead_of_blocking_same_process(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    db_path.touch()
    first = MaintenanceBarrierStore(tmp_path / "barrier", str(db_path))
    second = MaintenanceBarrierStore(tmp_path / "barrier", str(db_path))
    first.initialize(generation=1)
    result: list[str] = []

    def contend() -> None:
        try:
            second.read()
        except MaintenanceCoordinatorError as exc:
            result.append(exc.code)
        else:
            result.append("UNEXPECTED_SUCCESS")

    with first.guard():
        thread = threading.Thread(target=contend, daemon=True)
        thread.start()
        thread.join(timeout=0.2)
        assert result == ["BARRIER_BUSY"]
    thread.join(timeout=1)


def test_coordinator_prepares_all_live_runtimes_then_resumes_initially_open_set(tmp_path: Path) -> None:
    async def run() -> None:
        first = _identity(tmp_path, marker="first")
        second = _identity(tmp_path, marker="second")
        parked = _identity(tmp_path, marker="parked", state="PARKED")
        identities = [first, second, parked]
        registry = _registry(tmp_path, identities)
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", first.database_path)
        barrier.initialize(generation=1)
        states = {identity.instance_id: identity.lifecycle_state for identity in identities}
        calls: list[tuple[str, str]] = []
        exclusion_checks: list[str] = []

        async def call_runtime(identity: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            calls.append((identity.instance_id, request.command))
            state = states[identity.instance_id]
            if request.command == "INSPECT":
                return _response(
                    request,
                    state=state,
                    generation=identity.generation,
                    lease_open=state == "OPEN",
                )
            if request.command == "PREPARE_CLOSE":
                states[identity.instance_id] = "CLOSED"
                return _response(request, state="CLOSED", generation=1)
            if request.command == "RESUME":
                states[identity.instance_id] = "READY"
                return _response(request, state="READY", generation=request.expected_generation, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: exclusion_checks.append(path) or True,
            max_concurrency=2,
        )
        operation_id = str(uuid.uuid4())
        prepared = await coordinator.prepare_close(operation_id, drain_deadline=time.time() + 30)
        assert set(prepared.initially_open_instance_ids) == {first.instance_id, second.instance_id}
        assert states[first.instance_id] == "CLOSED"
        assert states[second.instance_id] == "CLOSED"
        assert states[parked.instance_id] == "PARKED"
        assert exclusion_checks == [first.database_path]
        assert barrier.read().state == "PREPARING"

        activating = coordinator.begin_activation(prepared, new_generation=2)
        assert activating.state == "ACTIVATING"
        assert activating.generation == 2
        assert barrier.read().state == "ACTIVATING"
        report = await coordinator.resume_activating(prepared, new_generation=2)
        assert report.failures == {}
        assert report.participant_failures == {}
        assert set(report.ready_instance_ids) == {first.instance_id, second.instance_id}
        assert states[first.instance_id] == "READY"
        assert states[second.instance_id] == "READY"
        assert states[parked.instance_id] == "PARKED"
        assert barrier.read().state == "ACTIVATING"
        coordinator.publish_running(prepared, new_generation=2)
        assert barrier.read().state == "RUNNING"
        assert barrier.read().generation == 2
        assert sum(command == "RESUME" for _, command in calls) == 2

    asyncio.run(run())


def test_shared_barrier_lifecycle_calls_are_ordered_even_when_inspection_can_fan_out(tmp_path: Path) -> None:
    async def run() -> None:
        first = _identity(tmp_path, marker="ordered-first")
        second = _identity(tmp_path, marker="ordered-second")
        registry = _registry(tmp_path, [first, second])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", first.database_path)
        barrier.initialize(generation=1)
        states = {first.instance_id: "OPEN", second.instance_id: "OPEN"}
        active_mutations = 0
        max_active_mutations = 0

        async def call_runtime(identity: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            nonlocal active_mutations, max_active_mutations
            if request.command == "INSPECT":
                await asyncio.sleep(0.005)
                return _response(request, state=states[identity.instance_id], generation=1, lease_open=True)
            if request.command in {"PREPARE_CLOSE", "ABORT", "RESUME"}:
                active_mutations += 1
                max_active_mutations = max(max_active_mutations, active_mutations)
                try:
                    await asyncio.sleep(0.005)
                    if request.command == "PREPARE_CLOSE":
                        states[identity.instance_id] = "CLOSED"
                        return _response(request, state="CLOSED", generation=1)
                    if request.command == "ABORT":
                        states[identity.instance_id] = "OPEN"
                        return _response(request, state="OPEN", generation=1, lease_open=True)
                    states[identity.instance_id] = "READY"
                    return _response(
                        request,
                        state="READY",
                        generation=request.expected_generation,
                        lease_open=True,
                    )
                finally:
                    active_mutations -= 1
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
            max_concurrency=8,
        )
        prepared = await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 30)
        coordinator.begin_activation(prepared, new_generation=2)
        coordinator.publish_running(prepared, new_generation=2)
        report = await coordinator.resume_running(prepared, new_generation=2)
        assert report.failures == {}
        assert report.participant_failures == {}
        assert set(report.ready_instance_ids) == {first.instance_id, second.instance_id}
        assert max_active_mutations == 1

    asyncio.run(run())


def test_resume_report_participant_failures_default_empty_and_instance_local() -> None:
    first = ResumeReport(("runtime-a",), {})
    second = ResumeReport(("runtime-b",), {"runtime-c": "UNREACHABLE"})

    assert first.participant_failures == {}
    assert second.participant_failures == {}
    assert first.participant_failures is not second.participant_failures


def test_resume_running_uses_absolute_runtime_resume_deadline_and_raises_typed_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, marker="resume-deadline")
        registry = _registry(tmp_path, [identity])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)

        async def call_runtime(target: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            assert target.instance_id == identity.instance_id
            if request.command == "INSPECT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                return _response(request, state="CLOSED", generation=1)
            if request.command == "RESUME":
                await asyncio.sleep(0.02)
                return _response(request, state="READY", generation=2, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        prepared = await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 30)
        coordinator.begin_activation(prepared, new_generation=2)
        coordinator.publish_running(prepared, new_generation=2)
        monkeypatch.setattr(
            maintenance_coordinator_module,
            "_RUNTIME_RESUME_TIMEOUT_SECONDS",
            0.005,
            raising=False,
        )

        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.resume_running(prepared, new_generation=2)

        assert exc_info.value.code == "RUNTIME_RESUME_TIMEOUT"
        assert barrier.read().state == "RUNNING"

    asyncio.run(run())


def test_resume_running_propagates_external_cancellation(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, marker="resume-cancellation")
        registry = _registry(tmp_path, [identity])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)
        resume_started = asyncio.Event()

        async def call_runtime(target: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            assert target.instance_id == identity.instance_id
            if request.command == "INSPECT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                return _response(request, state="CLOSED", generation=1)
            if request.command == "RESUME":
                resume_started.set()
                await asyncio.Event().wait()
                raise AssertionError("cancelled RESUME unexpectedly continued")
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        prepared = await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 30)
        coordinator.begin_activation(prepared, new_generation=2)
        coordinator.publish_running(prepared, new_generation=2)

        task = asyncio.create_task(coordinator.resume_running(prepared, new_generation=2))
        await resume_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_ordered_resume_uses_one_absolute_deadline_for_all_runtimes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        first = _identity(tmp_path, marker="resume-absolute-first")
        second = _identity(tmp_path, marker="resume-absolute-second")
        registry = _registry(tmp_path, [first, second])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", first.database_path)
        barrier.initialize(generation=1)
        resume_calls: list[str] = []

        async def call_runtime(identity: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            if request.command == "INSPECT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                return _response(request, state="CLOSED", generation=1)
            if request.command == "RESUME":
                resume_calls.append(identity.instance_id)
                await asyncio.sleep(0.035)
                return _response(request, state="READY", generation=2, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        prepared = await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 30)
        coordinator.begin_activation(prepared, new_generation=2)
        coordinator.publish_running(prepared, new_generation=2)
        monkeypatch.setattr(maintenance_coordinator_module, "_RUNTIME_RESUME_TIMEOUT_SECONDS", 0.055)

        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.resume_running(prepared, new_generation=2)

        assert exc_info.value.code == "RUNTIME_RESUME_TIMEOUT"
        assert 1 <= len(resume_calls) <= 2

    asyncio.run(run())


def test_ordered_prepare_close_uses_one_absolute_deadline_for_all_runtimes(tmp_path: Path) -> None:
    async def run() -> None:
        first = _identity(tmp_path, marker="deadline-first")
        second = _identity(tmp_path, marker="deadline-second")
        registry = _registry(tmp_path, [first, second])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", first.database_path)
        barrier.initialize(generation=1)
        states = {first.instance_id: "OPEN", second.instance_id: "OPEN"}

        async def call_runtime(identity: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            if request.command == "INSPECT":
                return _response(request, state=states[identity.instance_id], generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                await asyncio.sleep(0.035)
                states[identity.instance_id] = "CLOSED"
                return _response(request, state="CLOSED", generation=1)
            if request.command == "ABORT":
                states[identity.instance_id] = "OPEN"
                return _response(request, state="OPEN", generation=1, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
            max_concurrency=8,
        )
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 0.055)
        assert exc_info.value.code == "CLIENT_DRAIN_TIMEOUT"
        assert barrier.read().state == "RECOVERY_REQUIRED"
        assert sorted(states.values()) == ["CLOSED", "OPEN"]

    asyncio.run(run())


def test_prepare_close_hung_inspect_is_bounded_and_leaves_recovery_required(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, marker="hung-inspect")
        registry = _registry(tmp_path, [identity])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)
        calls: list[str] = []

        async def call_runtime(target: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            calls.append(request.command)
            if request.command == "INSPECT":
                await asyncio.Event().wait()
                raise AssertionError("hung INSPECT unexpectedly resumed")
            if request.command == "ABORT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        started = time.monotonic()
        try:
            with pytest.raises(MaintenanceCoordinatorError) as exc_info:
                await asyncio.wait_for(
                    coordinator.prepare_close(
                        str(uuid.uuid4()),
                        drain_deadline=time.time() + 0.04,
                    ),
                    timeout=0.2,
                )
        except TimeoutError:
            pytest.fail("hung INSPECT exceeded the bounded prepare_close deadline")
        elapsed = time.monotonic() - started

        assert exc_info.value.code == "CLIENT_DRAIN_TIMEOUT"
        assert elapsed < 0.15
        assert calls == ["INSPECT"]
        assert barrier.read().state == "RECOVERY_REQUIRED"
        assert barrier.read().generation == 1

    asyncio.run(run())


def test_inspect_time_consumes_same_deadline_before_ordered_close(tmp_path: Path) -> None:
    async def run() -> None:
        first = _identity(tmp_path, marker="inspect-budget-first")
        second = _identity(tmp_path, marker="inspect-budget-second")
        registry = _registry(tmp_path, [first, second])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", first.database_path)
        barrier.initialize(generation=1)
        states = {first.instance_id: "OPEN", second.instance_id: "OPEN"}
        calls: list[tuple[str, str]] = []

        async def call_runtime(identity: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            calls.append((identity.instance_id, request.command))
            if request.command == "INSPECT":
                await asyncio.sleep(0.05)
                return _response(request, state="OPEN", generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                assert request.drain_deadline == drain_deadline
                await asyncio.sleep(0.18)
                states[identity.instance_id] = "CLOSED"
                return _response(request, state="CLOSED", generation=1)
            if request.command == "ABORT":
                states[identity.instance_id] = "OPEN"
                return _response(request, state="OPEN", generation=1, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        drain_deadline = time.time() + 0.2
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close(
                str(uuid.uuid4()),
                drain_deadline=drain_deadline,
            )

        assert exc_info.value.code == "CLIENT_DRAIN_TIMEOUT"
        assert sum(command == "INSPECT" for _, command in calls) == 2
        # INSPECT consumes part of the one absolute deadline. The first ordered close
        # therefore has less than its 180ms fixture duration remaining and is
        # cancelled at that same deadline; a second close must never start.
        assert sum(command == "PREPARE_CLOSE" for _, command in calls) == 1
        assert sorted(states.values()) == ["OPEN", "OPEN"]
        assert all(command != "ABORT" for _, command in calls)
        assert barrier.read().state == "RECOVERY_REQUIRED"

        # Cancellation at the absolute deadline must also prevent a late fixture
        # completion from changing runtime state after prepare_close has returned.
        await asyncio.sleep(0.2)
        assert sorted(states.values()) == ["OPEN", "OPEN"]
        assert all(command != "ABORT" for _, command in calls)

    asyncio.run(run())


def test_timeout_reconciliation_uses_only_remaining_deadline_and_can_restore_running(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, marker="early-timeout-clean-abort")
        registry = _registry(tmp_path, [identity])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)
        calls: list[str] = []

        async def call_runtime(target: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            calls.append(request.command)
            if request.command == "INSPECT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                raise MaintenanceCoordinatorError("CLIENT_DRAIN_TIMEOUT", "fixture early timeout")
            if request.command == "ABORT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close(
                str(uuid.uuid4()),
                drain_deadline=time.time() + 0.2,
            )

        assert exc_info.value.code == "CLIENT_DRAIN_TIMEOUT"
        assert calls == ["INSPECT", "PREPARE_CLOSE", "ABORT"]
        assert barrier.read().state == "RUNNING"
        assert barrier.read().generation == 1

    asyncio.run(run())


def test_timeout_abort_reconciliation_cannot_wait_past_absolute_deadline(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, marker="early-timeout-hung-abort")
        registry = _registry(tmp_path, [identity])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)
        calls: list[str] = []

        async def call_runtime(target: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            calls.append(request.command)
            if request.command == "INSPECT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                raise MaintenanceCoordinatorError("CLIENT_DRAIN_TIMEOUT", "fixture early timeout")
            if request.command == "ABORT":
                await asyncio.Event().wait()
                raise AssertionError("hung ABORT unexpectedly resumed")
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        started = time.monotonic()
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close(
                str(uuid.uuid4()),
                drain_deadline=time.time() + 0.04,
            )
        elapsed = time.monotonic() - started

        assert exc_info.value.code == "CLIENT_DRAIN_TIMEOUT"
        assert elapsed < 0.15
        assert calls == ["INSPECT", "PREPARE_CLOSE", "ABORT"]
        assert barrier.read().state == "RECOVERY_REQUIRED"

    asyncio.run(run())


def test_activation_barrier_binds_new_inode_and_recovery_only_from_preparing_or_activating(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    db_path.write_bytes(b"one")
    store = MaintenanceBarrierStore(tmp_path / "barrier", str(db_path))
    store.initialize(generation=3)
    operation_id = str(uuid.uuid4())
    store.begin_prepare(operation_id, expected_generation=3)

    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"two")
    os.replace(replacement, db_path)
    activating = store.begin_activation(operation_id, generation=4)
    live = db_path.stat()
    assert activating.state == "ACTIVATING"
    assert (activating.device, activating.inode) == (live.st_dev, live.st_ino)

    recovery = store.mark_recovery_required(operation_id)
    assert recovery.state == "RECOVERY_REQUIRED"
    assert recovery.generation == 4

    other = tmp_path / "other.db"
    other.touch()
    running_store = MaintenanceBarrierStore(tmp_path / "running-barrier", str(other))
    running_store.initialize(generation=1)
    with pytest.raises(MaintenanceCoordinatorError) as exc_info:
        running_store.mark_recovery_required(str(uuid.uuid4()))
    assert exc_info.value.code in {"OPERATION_MISMATCH", "INVALID_BARRIER_TRANSITION"}

    operation_2 = str(uuid.uuid4())
    running_store.begin_prepare(operation_2, expected_generation=1)
    running_store.begin_activation(operation_2, generation=2)
    running_store.publish_running(operation_2, generation=2)
    with pytest.raises(MaintenanceCoordinatorError) as exc_info:
        running_store.mark_recovery_required(operation_2)
    assert exc_info.value.code == "OPERATION_MISMATCH"


def test_activation_ready_reconcile_only_finishes_exact_activating_identity(tmp_path: Path) -> None:
    identity = _identity(tmp_path, marker="only")
    registry = _registry(tmp_path, [identity])
    barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
    barrier.initialize(generation=1)
    coordinator = MaintenanceCoordinator(
        barrier=barrier,
        registry=registry,
        call_runtime=lambda target, request: asyncio.sleep(0),
        exclusion_check=lambda path: True,
    )
    operation_id = str(uuid.uuid4())
    barrier.begin_prepare(operation_id, expected_generation=1)
    activation = barrier.begin_activation(operation_id, generation=2)

    running = coordinator.reconcile_activation_ready(
        operation_id,
        new_generation=2,
        device=activation.device,
        inode=activation.inode,
    )
    assert running.state == "RUNNING"
    assert running.generation == 2

    with pytest.raises(MaintenanceCoordinatorError) as exc_info:
        coordinator.reconcile_activation_ready(
            operation_id,
            new_generation=3,
            device=activation.device,
            inode=activation.inode,
        )
    assert exc_info.value.code == "ACTIVATION_IDENTITY_MISMATCH"


def test_failed_close_is_aborted_before_mutation_and_original_generation_reopens(tmp_path: Path) -> None:
    async def run() -> None:
        first = _identity(tmp_path, marker="first")
        second = _identity(tmp_path, marker="second")
        registry = _registry(tmp_path, [first, second])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", first.database_path)
        barrier.initialize(generation=1)
        states = {first.instance_id: "OPEN", second.instance_id: "OPEN"}
        aborts: list[str] = []

        async def call_runtime(identity: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            if request.command == "INSPECT":
                return _response(request, state=states[identity.instance_id], generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                if identity.instance_id == second.instance_id:
                    return _response(request, state="CLOSE_FAILED", generation=1, status_code="CLOSE_FAILED", lease_open=True)
                states[identity.instance_id] = "CLOSED"
                return _response(request, state="CLOSED", generation=1)
            if request.command == "ABORT":
                aborts.append(identity.instance_id)
                states[identity.instance_id] = "OPEN"
                return _response(request, state="OPEN", generation=1, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 30)
        assert exc_info.value.code == "CLIENT_CLOSE_FAILED"
        assert set(aborts) == {first.instance_id, second.instance_id}
        assert barrier.read().state == "RUNNING"
        assert barrier.read().generation == 1
        assert states == {first.instance_id: "OPEN", second.instance_id: "OPEN"}

    asyncio.run(run())


def test_inspection_failure_reconciles_but_expired_drain_marks_recovery_required(tmp_path: Path) -> None:
    async def run_inspection_failure() -> None:
        identity = _identity(tmp_path / "inspect", marker="inspect")
        registry = _registry(tmp_path / "inspect", [identity])
        barrier = MaintenanceBarrierStore(tmp_path / "inspect" / "barrier", identity.database_path)
        barrier.initialize(generation=1)

        async def call_runtime(target: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            if request.command == "INSPECT":
                raise OSError("fixture unavailable")
            if request.command == "ABORT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 30)
        assert exc_info.value.code == "CLIENT_INSPECTION_FAILED"
        assert barrier.read().state == "RUNNING"
        assert barrier.read().generation == 1

    async def run_drain_timeout() -> None:
        identity = _identity(tmp_path / "drain", marker="drain")
        registry = _registry(tmp_path / "drain", [identity])
        barrier = MaintenanceBarrierStore(tmp_path / "drain" / "barrier", identity.database_path)
        barrier.initialize(generation=1)

        async def call_runtime(target: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            if request.command == "INSPECT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                await asyncio.sleep(0.05)
                return _response(request, state="CLOSED", generation=1)
            if request.command == "ABORT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 0.01)
        assert exc_info.value.code == "CLIENT_DRAIN_TIMEOUT"
        assert barrier.read().state == "RECOVERY_REQUIRED"
        assert barrier.read().generation == 1

    asyncio.run(run_inspection_failure())
    asyncio.run(run_drain_timeout())


def test_unproven_handle_exclusion_aborts_and_stale_registry_blocks_before_barrier(tmp_path: Path) -> None:
    async def run_exclusion() -> None:
        identity = _identity(tmp_path, marker="only")
        registry = _registry(tmp_path, [identity])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)
        state = "OPEN"

        async def call_runtime(target: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            nonlocal state
            if request.command == "INSPECT":
                return _response(request, state=state, generation=1, lease_open=state == "OPEN")
            if request.command == "PREPARE_CLOSE":
                state = "CLOSED"
                return _response(request, state="CLOSED", generation=1)
            if request.command == "ABORT":
                state = "OPEN"
                return _response(request, state="OPEN", generation=1, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: False,
        )
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 30)
        assert exc_info.value.code == "EXCLUSION_UNPROVEN"
        assert state == "OPEN"
        assert barrier.read().state == "RUNNING"

    async def run_stale() -> None:
        stale = _identity(tmp_path / "stale", marker="stale")
        registry = RuntimeRegistry(
            tmp_path / "stale" / "runtime",
            owner_uid=os.getuid(),
            boot_id_provider=lambda: "boot-a",
            process_start_provider=lambda pid: 999,
        )
        registry.register(stale)
        barrier = MaintenanceBarrierStore(tmp_path / "stale" / "barrier", stale.database_path)
        barrier.initialize(generation=1)
        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=lambda identity, request: asyncio.sleep(0),
            exclusion_check=lambda path: True,
        )
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 30)
        assert exc_info.value.code == "REGISTRY_IDENTITY_INVALID"
        assert barrier.read().state == "RUNNING"

    asyncio.run(run_exclusion())
    asyncio.run(run_stale())


def test_recover_aborted_pre_mutation_only_accepts_exact_running_maintenance_identity(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, marker="pre-mutation-recovery")
        registry = _registry(tmp_path, [identity])
        barrier = MaintenanceBarrierStore(tmp_path / "barrier-recover", identity.database_path)
        barrier.initialize(generation=1)

        async def call_runtime(target: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
            assert target.instance_id == identity.instance_id
            if request.command == "INSPECT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            if request.command == "PREPARE_CLOSE":
                return _response(request, state="CLOSED", generation=1)
            if request.command == "ABORT":
                return _response(request, state="OPEN", generation=1, lease_open=True)
            raise AssertionError(request.command)

        coordinator = MaintenanceCoordinator(
            barrier=barrier,
            registry=registry,
            call_runtime=call_runtime,
            exclusion_check=lambda path: True,
        )
        prepared = await coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 30)
        await coordinator.abort_pre_mutation(prepared)

        recovered = await coordinator.recover_aborted_pre_mutation(prepared)
        assert recovered.state == "RUNNING"
        assert recovered.operation_id == prepared.operation_id
        assert recovered.generation == prepared.original_generation
        assert recovered.original_generation == prepared.original_generation
        assert recovered.intent_revision == prepared.intent_revision

        mismatched = PreparedMaintenance(
            operation_id=prepared.operation_id,
            original_generation=prepared.original_generation,
            intent_revision=prepared.intent_revision + 1,
            initially_open_instance_ids=prepared.initially_open_instance_ids,
            registrations=prepared.registrations,
        )
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.recover_aborted_pre_mutation(mismatched)
        assert exc_info.value.code == "PREMUTATION_RECOVERY_MISMATCH"

        registry.register(identity.with_state("CLOSED"))
        with pytest.raises(MaintenanceCoordinatorError) as stale_info:
            await coordinator.recover_aborted_pre_mutation(prepared)
        assert stale_info.value.code == "PREMUTATION_RECOVERY_MISMATCH"

    asyncio.run(run())
