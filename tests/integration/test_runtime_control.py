from __future__ import annotations

import asyncio
import json
import os
import stat
import time
import uuid
from pathlib import Path

import pytest

from factlane.maintenance_coordinator import MaintenanceBarrierStore, MaintenanceCoordinatorError
from factlane.runtime_control import (
    MAX_CONTROL_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    RuntimeControlError,
    RuntimeControlClient,
    RuntimeControlRequest,
    RuntimeControlServer,
    RuntimeIdentity,
    RuntimeLifecycleController,
    RuntimeRegistry,
    decode_request,
)


def _identity(
    tmp_path: Path,
    *,
    instance_id: str | None = None,
    generation: int = 1,
    pid: int | None = None,
    start_ticks: int = 101,
    boot_id: str = "boot-a",
) -> RuntimeIdentity:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    db_path = tmp_path / "memory.db"
    db_path.touch(exist_ok=True)
    db_stat = db_path.stat()
    return RuntimeIdentity(
        protocol_version=PROTOCOL_VERSION,
        instance_id=instance_id or str(uuid.uuid4()),
        owner_uid=os.getuid(),
        pid=os.getpid() if pid is None else pid,
        process_start_ticks=start_ticks,
        boot_id=boot_id,
        database_path=str(db_path.resolve()),
        device=db_stat.st_dev,
        inode=db_stat.st_ino,
        generation=generation,
        control_socket_path=str(runtime_dir / f"{instance_id or 'runtime'}.sock"),
        lifecycle_state="PARKED",
    )


def _request(
    identity: RuntimeIdentity,
    *,
    command: str,
    operation_id: str,
    generation: int | None = None,
    drain_deadline: float | None = None,
) -> RuntimeControlRequest:
    payload: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "operation_id": operation_id,
        "instance_id": identity.instance_id,
        "expected_generation": identity.generation if generation is None else generation,
        "command": command,
    }
    if drain_deadline is not None:
        payload["drain_deadline"] = drain_deadline
    return RuntimeControlRequest.from_mapping(payload)


def test_control_request_is_strict_bounded_ndjson_and_has_no_memory_commands(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    operation_id = str(uuid.uuid4())
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "operation_id": operation_id,
        "instance_id": identity.instance_id,
        "expected_generation": 1,
        "command": "PREPARE_CLOSE",
        "drain_deadline": time.time() + 30,
    }
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    request = decode_request(encoded)
    assert request.command == "PREPARE_CLOSE"
    assert request.operation_id == operation_id

    for changed, code in (
        ({**payload, "command": "memory_search"}, "UNSUPPORTED_COMMAND"),
        ({**payload, "database_path": str(tmp_path / "other.db")}, "INVALID_CONTROL_MESSAGE"),
        ({**payload, "protocol_version": 2}, "UNSUPPORTED_PROTOCOL"),
    ):
        raw = (json.dumps(changed, separators=(",", ":")) + "\n").encode("utf-8")
        with pytest.raises(RuntimeControlError) as exc_info:
            decode_request(raw)
        assert exc_info.value.code == code

    oversized = b"{" + b" " * MAX_CONTROL_MESSAGE_BYTES + b"}\n"
    with pytest.raises(RuntimeControlError) as exc_info:
        decode_request(oversized)
    assert exc_info.value.code == "CONTROL_MESSAGE_TOO_LARGE"


def test_registry_is_owner_private_and_rejects_stale_or_forged_process_identity(tmp_path: Path) -> None:
    identity = _identity(tmp_path, start_ticks=777, boot_id="boot-a")
    starts = {identity.pid: identity.process_start_ticks}
    registry = RuntimeRegistry(
        tmp_path / "runtime",
        owner_uid=os.getuid(),
        boot_id_provider=lambda: "boot-a",
        process_start_provider=lambda pid: starts.get(pid),
    )
    registry.register(identity)

    assert stat.S_IMODE((tmp_path / "runtime").stat().st_mode) == 0o700
    registration_path = tmp_path / "runtime" / "registry" / f"{identity.instance_id}.json"
    assert stat.S_IMODE(registration_path.stat().st_mode) == 0o600
    assert registry.validate_live(identity, peer_uid=os.getuid(), peer_pid=identity.pid) == identity

    with pytest.raises(RuntimeControlError) as exc_info:
        registry.validate_live(identity, peer_uid=os.getuid() + 1, peer_pid=identity.pid)
    assert exc_info.value.code == "PEER_UID_MISMATCH"

    starts[identity.pid] += 1
    with pytest.raises(RuntimeControlError) as exc_info:
        registry.validate_live(identity, peer_uid=os.getuid(), peer_pid=identity.pid)
    assert exc_info.value.code == "STALE_PROCESS_IDENTITY"

    stale_boot_registry = RuntimeRegistry(
        tmp_path / "runtime",
        owner_uid=os.getuid(),
        boot_id_provider=lambda: "boot-b",
        process_start_provider=lambda pid: identity.process_start_ticks,
    )
    with pytest.raises(RuntimeControlError) as exc_info:
        stale_boot_registry.validate_live(identity)
    assert exc_info.value.code == "STALE_BOOT_IDENTITY"


def test_private_unix_control_socket_round_trip_validates_registered_runtime_identity(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, start_ticks=313)
        registry = RuntimeRegistry(
            tmp_path / "runtime",
            owner_uid=os.getuid(),
            boot_id_provider=lambda: identity.boot_id,
            process_start_provider=lambda pid: identity.process_start_ticks if pid == identity.pid else None,
        )
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)
        opened = False

        async def open_adapter(generation: int) -> None:
            nonlocal opened
            opened = True

        async def close_adapter() -> None:
            nonlocal opened
            opened = False

        controller = RuntimeLifecycleController(
            identity,
            barrier=barrier,
            registry=registry,
            open_adapter=open_adapter,
            close_adapter=close_adapter,
            lease_is_open=lambda: opened,
        )
        server = RuntimeControlServer(identity=identity, registry=registry, controller=controller)
        await server.start()
        try:
            with pytest.raises(RuntimeControlError) as exc_info:
                registry.load(identity.instance_id)
            assert exc_info.value.code == "REGISTRATION_NOT_FOUND"
            await controller.startup_open()
            socket_path = Path(identity.control_socket_path)
            assert socket_path.exists()
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
            client = RuntimeControlClient(registry)
            operation_id = str(uuid.uuid4())
            response = await client.call(
                controller.identity,
                _request(controller.identity, command="INSPECT", operation_id=operation_id),
            )
            assert response["status_code"] == "OK"
            assert response["state"] == "OPEN"
            assert response["lease_open"] is True
        finally:
            await server.close()

    asyncio.run(run())


def test_lifecycle_prepare_abort_resume_are_generation_bound_and_idempotent(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, generation=1)
        registry = RuntimeRegistry(
            tmp_path / "runtime",
            owner_uid=os.getuid(),
            boot_id_provider=lambda: identity.boot_id,
            process_start_provider=lambda pid: identity.process_start_ticks if pid == identity.pid else None,
        )
        registry.register(identity)
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)

        opened = False
        open_generations: list[int] = []
        close_calls = 0

        async def open_adapter(generation: int) -> None:
            nonlocal opened
            open_generations.append(generation)
            opened = True

        async def close_adapter() -> None:
            nonlocal opened, close_calls
            close_calls += 1
            opened = False

        controller = RuntimeLifecycleController(
            identity,
            barrier=barrier,
            registry=registry,
            open_adapter=open_adapter,
            close_adapter=close_adapter,
            lease_is_open=lambda: opened,
        )
        assert await controller.startup_open() == "OPEN"

        operation_1 = str(uuid.uuid4())
        barrier.begin_prepare(operation_1, expected_generation=1)
        prepare_1 = _request(
            controller.identity,
            command="PREPARE_CLOSE",
            operation_id=operation_1,
            drain_deadline=time.time() + 30,
        )
        first = await controller.handle(prepare_1)
        duplicate = await controller.handle(prepare_1)
        assert first == duplicate
        assert first["status_code"] == "OK"
        assert first["state"] == "CLOSED"
        assert close_calls == 1
        assert controller.admits_normal_requests is False

        retried = RuntimeControlRequest.from_mapping(
            {**prepare_1.to_mapping(), "request_id": str(uuid.uuid4())}
        )
        retried_response = await controller.handle(retried)
        assert retried_response["request_id"] == retried.request_id
        assert retried_response["state"] == "CLOSED"
        assert close_calls == 1

        conflicting = RuntimeControlRequest.from_mapping(
            {**prepare_1.to_mapping(), "drain_deadline": prepare_1.drain_deadline + 1}
        )
        conflict = await controller.handle(conflicting)
        assert conflict["status_code"] == "IDEMPOTENCY_CONFLICT"
        assert close_calls == 1

        barrier.publish_abort(operation_1)
        abort = _request(controller.identity, command="ABORT", operation_id=operation_1, generation=1)
        aborted = await controller.handle(abort)
        assert aborted["status_code"] == "OK"
        assert aborted["state"] == "OPEN"
        assert open_generations == [1, 1]
        barrier.publish_running(operation_1, generation=1)

        operation_2 = str(uuid.uuid4())
        barrier.begin_prepare(operation_2, expected_generation=1)
        prepare_2 = _request(
            controller.identity,
            command="PREPARE_CLOSE",
            operation_id=operation_2,
            drain_deadline=time.time() + 30,
        )
        assert (await controller.handle(prepare_2))["state"] == "CLOSED"
        barrier.begin_activation(operation_2, generation=2)
        resume = _request(controller.identity, command="RESUME", operation_id=operation_2, generation=2)
        resumed = await controller.handle(resume)
        assert resumed["status_code"] == "OK"
        assert resumed["state"] == "READY"
        assert resumed["actual_generation"] == 2
        assert controller.identity.generation == 2
        assert open_generations == [1, 1, 2]
        assert barrier.read().state == "ACTIVATING"
        assert controller.admits_normal_requests is False
        assert await controller.handle(resume) == resumed
        assert open_generations == [1, 1, 2]
        barrier.publish_running(operation_2, generation=2)
        assert controller.admits_normal_requests is True

    asyncio.run(run())


def test_startup_under_activating_stays_parked_and_does_not_open_adapter(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, generation=2)
        registry = RuntimeRegistry(
            tmp_path / "runtime",
            owner_uid=os.getuid(),
            boot_id_provider=lambda: identity.boot_id,
            process_start_provider=lambda pid: identity.process_start_ticks if pid == identity.pid else None,
        )
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)
        operation_id = str(uuid.uuid4())
        barrier.begin_prepare(operation_id, expected_generation=1)
        barrier.begin_activation(operation_id, generation=2)
        open_calls: list[int] = []

        async def open_adapter(generation: int) -> None:
            open_calls.append(generation)

        controller = RuntimeLifecycleController(
            identity,
            barrier=barrier,
            registry=registry,
            open_adapter=open_adapter,
            close_adapter=lambda: asyncio.sleep(0),
            lease_is_open=lambda: False,
        )
        assert await controller.startup_open() == "PARKED"
        assert open_calls == []
        assert controller.admits_normal_requests is False

    asyncio.run(run())


def test_startup_attachment_holds_barrier_before_prepare_and_close_serializes_after_open(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, generation=1)
        registry = RuntimeRegistry(
            tmp_path / "runtime",
            owner_uid=os.getuid(),
            boot_id_provider=lambda: identity.boot_id,
            process_start_provider=lambda pid: identity.process_start_ticks if pid == identity.pid else None,
        )
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)
        opened = False
        open_started = asyncio.Event()
        allow_open = asyncio.Event()
        events: list[str] = []

        async def open_adapter(generation: int) -> None:
            nonlocal opened
            events.append("open-start")
            open_started.set()
            await allow_open.wait()
            opened = True
            events.append("open-done")

        async def close_adapter() -> None:
            nonlocal opened
            events.append("close")
            opened = False

        controller = RuntimeLifecycleController(
            identity,
            barrier=barrier,
            registry=registry,
            open_adapter=open_adapter,
            close_adapter=close_adapter,
            lease_is_open=lambda: opened,
        )
        startup = asyncio.create_task(controller.startup_open())
        await open_started.wait()
        operation_id = str(uuid.uuid4())
        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            barrier.begin_prepare(operation_id, expected_generation=1)
        assert exc_info.value.code == "BARRIER_BUSY"

        allow_open.set()
        assert await startup == "OPEN"
        barrier.begin_prepare(operation_id, expected_generation=1)
        response = await controller.handle(
            _request(
                controller.identity,
                command="PREPARE_CLOSE",
                operation_id=operation_id,
                drain_deadline=time.time() + 30,
            )
        )
        assert response["state"] == "CLOSED"
        assert events == ["open-start", "open-done", "close"]

    asyncio.run(run())


def test_prepare_close_never_claims_closed_while_lease_is_still_open(tmp_path: Path) -> None:
    async def run() -> None:
        identity = _identity(tmp_path, generation=1)
        registry = RuntimeRegistry(
            tmp_path / "runtime",
            owner_uid=os.getuid(),
            boot_id_provider=lambda: identity.boot_id,
            process_start_provider=lambda pid: identity.process_start_ticks if pid == identity.pid else None,
        )
        registry.register(identity)
        barrier = MaintenanceBarrierStore(tmp_path / "barrier", identity.database_path)
        barrier.initialize(generation=1)

        opened = True

        async def close_adapter() -> None:
            return None

        controller = RuntimeLifecycleController(
            identity.with_state("OPEN"),
            barrier=barrier,
            registry=registry,
            open_adapter=lambda generation: asyncio.sleep(0),
            close_adapter=close_adapter,
            lease_is_open=lambda: opened,
        )
        operation_id = str(uuid.uuid4())
        barrier.begin_prepare(operation_id, expected_generation=1)
        response = await controller.handle(
            _request(
                controller.identity,
                command="PREPARE_CLOSE",
                operation_id=operation_id,
                drain_deadline=time.time() + 30,
            )
        )
        assert response["status_code"] == "CLOSE_FAILED"
        assert response["state"] == "CLOSE_FAILED"
        assert controller.admits_normal_requests is False

    asyncio.run(run())
