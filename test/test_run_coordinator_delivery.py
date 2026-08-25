"""Transactional outbox delivery and retry behavior."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.run_coordinator import (
    CommandOperation,
    CoordinatorDecision,
    CoordinatorReason,
    CoordinatorResult,
    DeliveryState,
    MemoryRunCoordinator,
    OwnerLease,
    RunCompletion,
    RunFence,
    RunOutcome,
    SubmitRun,
)
from kiro_crew.run_coordinator.delivery import DeliveryAttempt, OutboxDeliveryAdapter
from kiro_crew.subagent import SubagentInfo, SubagentManager


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def terminal_retry_manager(monkeypatch):
    monkeypatch.setattr("kiro_crew.subagent._TERMINAL_RETRY_SECONDS", 0.0)
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    info = SubagentInfo(id="run-retry", task="inspect", done=True)
    info._coordinator_fence = MagicMock()
    event = MagicMock(event_id="event-retry")
    return manager, info, event


@pytest.mark.asyncio
async def test_terminal_report_retries_transient_coordinator_commit(terminal_retry_manager) -> None:
    manager, info, event = terminal_retry_manager
    authority_stop = AsyncMock()
    manager.command_authority.stop_execution_heartbeat = authority_stop

    async def commit_then_succeed(_info: SubagentInfo):
        assert not authority_stop.await_count
        return None if manager._commit_terminal_event.await_count == 1 else event

    manager._commit_terminal_event = AsyncMock(side_effect=commit_then_succeed)
    manager._outbox_delivery.drain_once = AsyncMock(
        return_value=[DeliveryAttempt(event.event_id, DeliveryState.DELIVERED)]
    )

    await manager._report_terminal(
        info,
        source="test",
        injection_timeout_reason="timeout",
        mark_delivered_on_success=True,
    )

    assert manager._commit_terminal_event.await_count == 2
    authority_stop.assert_awaited_once_with(info.id)
    manager._outbox_delivery.drain_once.assert_awaited_once_with(event_id=event.event_id)


@pytest.mark.asyncio
async def test_terminal_report_stops_after_permanent_fence_rejection(
    terminal_retry_manager,
    monkeypatch,
) -> None:
    manager, info, _event = terminal_retry_manager
    cleared: list[str] = []
    monkeypatch.setattr("kiro_crew.subagent.clear_tombstone", cleared.append)
    manager._coordinator.complete = AsyncMock(
        return_value=CoordinatorResult(
            CoordinatorDecision.REJECTED,
            CoordinatorReason.STALE_FENCE,
            None,
        )
    )
    manager._outbox_delivery.drain_once = AsyncMock()

    await asyncio.wait_for(
        manager._report_terminal(
            info,
            source="test",
            injection_timeout_reason="timeout",
            mark_delivered_on_success=True,
        ),
        timeout=0.2,
    )

    manager._coordinator.complete.assert_awaited_once()
    manager._outbox_delivery.drain_once.assert_not_awaited()
    assert cleared == [info.id]


@pytest.mark.asyncio
async def test_terminal_report_retries_transient_outbox_routing(terminal_retry_manager) -> None:
    manager, info, event = terminal_retry_manager
    manager._commit_terminal_event = AsyncMock(return_value=event)
    manager._outbox_delivery.drain_once = AsyncMock(
        side_effect=[
            RuntimeError("routing unavailable"),
            [DeliveryAttempt(event.event_id, DeliveryState.DELIVERED)],
        ]
    )

    await manager._report_terminal(
        info,
        source="test",
        injection_timeout_reason="timeout",
        mark_delivered_on_success=True,
    )

    assert manager._outbox_delivery.drain_once.await_count == 2


async def _completed_event(coordinator: MemoryRunCoordinator, clock: _Clock, run_id: str):
    submitted = await coordinator.submit(
        SubmitRun(
            run_id=run_id,
            command_id=f"command-{run_id}",
            idempotency_key=f"key-{run_id}",
            payload_hash=f"hash-{run_id}",
            payload_json="{}",
            parent_session="dashboard:parent",
            agent="reviewer",
            task="inspect",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        submitted.value.command.command_id,
        OwnerLease("executor", clock.value + 30),
    )
    assert claim is not None and claim.fence is not None and claim.run is not None
    starting = await coordinator.mark_starting(
        claim.command,
        claim.fence,
        claim.run.version,
    )
    assert starting.value is not None
    running = await coordinator.mark_running(
        run_id,
        claim.fence,
        starting.value.version,
    )
    assert running.value is not None
    completed = await coordinator.complete(
        RunCompletion(
            run_id=run_id,
            outcome=RunOutcome.COMPLETED,
            result_path=f"/results/{run_id}.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json=json.dumps({"id": run_id, "result_summary": "done"}),
            terminal_at=clock.value,
        ),
        claim.fence,
        running.value.version,
    )
    assert completed.value is not None
    return completed.value


@pytest.mark.asyncio
async def test_startup_reconciliation_drains_completion_committed_before_restart(
    monkeypatch,
) -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    delivered = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=delivered,
        coordinator=coordinator,
    )
    monkeypatch.setattr("kiro_crew.subagent.list_orphans", lambda: [])

    await manager._reconcile_orphans()

    delivered.assert_awaited_once()
    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED


@pytest.mark.asyncio
async def test_live_coordinator_delivery_retains_batch_context() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-live-batch")
    event = await _completed_event(coordinator, clock, "run-live-batch")
    delivered: list[tuple[str, int, str]] = []

    async def capture(info: SubagentInfo) -> None:
        delivered.append((info.batch_id, info.batch_total, info._delivery_event_id))

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=capture,
        coordinator=coordinator,
    )

    manager.prepare_coordinator_rejection(
        event.run_id,
        batch_id="wave-live",
        batch_total=3,
    )
    await manager._outbox_delivery.drain_once(event_id=event.event_id)

    assert delivered == [("wave-live", 3, event.event_id)]
    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED


@pytest.mark.asyncio
async def test_startup_reconciliation_does_not_reprocess_deferred_outbox_as_orphan(
    monkeypatch,
) -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")

    async def defer(info: SubagentInfo) -> None:
        info._delivery_queued = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=defer,
        coordinator=coordinator,
    )
    tombstone = MagicMock()
    notify = AsyncMock()
    monkeypatch.setattr("kiro_crew.subagent.list_orphans", lambda: [{"id": event.run_id}])
    monkeypatch.setattr("kiro_crew.subagent.write_tombstone", tombstone)
    monkeypatch.setattr(manager, "_notify_orphan", notify)

    await manager._reconcile_orphans()

    assert coordinator._outbox[event.event_id].status is DeliveryState.PENDING
    tombstone.assert_not_called()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_reconciliation_drains_every_eligible_outbox_batch(
    monkeypatch,
) -> None:
    clock = _Clock()
    event_ids = iter(f"event-{index}" for index in range(17))
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: next(event_ids))
    events = [await _completed_event(coordinator, clock, f"run-{index}") for index in range(17)]
    remaining = {event.run_id for event in events}
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=AsyncMock(),
        coordinator=coordinator,
    )
    tombstone = MagicMock()
    notify = AsyncMock()
    monkeypatch.setattr(
        "kiro_crew.subagent.list_orphans",
        lambda: [{"id": run_id} for run_id in sorted(remaining)],
    )
    monkeypatch.setattr(
        "kiro_crew.subagent.mark_delivered",
        lambda run_id: remaining.discard(run_id),
    )
    monkeypatch.setattr("kiro_crew.subagent.write_tombstone", tombstone)
    monkeypatch.setattr(manager, "_notify_orphan", notify)

    await manager._reconcile_orphans()

    assert remaining == set()
    tombstone.assert_not_called()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_outbox_context_is_not_routed_twice() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    delivered = AsyncMock()

    async def defer(info: SubagentInfo) -> None:
        info._delivery_queued = True
        await delivered(info)

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=defer,
        coordinator=coordinator,
    )
    manager._fire_event = AsyncMock()

    assert await manager._deliver_outbox_event(event) is False
    assert await manager._deliver_outbox_event(event) is False

    delivered.assert_awaited_once()
    manager._fire_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_parent_injection_keeps_outbox_pending() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")

    async def fail_delivery(info: SubagentInfo) -> None:
        info._delivery_failed = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=fail_delivery,
        coordinator=coordinator,
    )

    accepted = await manager._deliver_outbox_event(event)

    assert accepted is False
    assert event.event_id in manager._outbox_contexts


@pytest.mark.asyncio
async def test_parent_retry_reuses_context_without_repeating_lifecycle_event() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    callbacks = 0

    async def fail_once(info: SubagentInfo) -> None:
        nonlocal callbacks
        callbacks += 1
        if callbacks == 1:
            info._delivery_failed = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=fail_once,
        coordinator=coordinator,
    )
    manager._fire_event = AsyncMock()

    assert await manager._deliver_outbox_event(event) is False
    assert await manager._deliver_outbox_event(event) is True
    assert callbacks == 2
    manager._fire_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_reaper_retries_pending_completion_delivery(monkeypatch) -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=AsyncMock(),
        coordinator=coordinator,
    )
    manager._conv_registry_rebuilt = True
    monkeypatch.setattr("kiro_crew.subagent.compact_cost_log", MagicMock())
    monkeypatch.setattr(manager, "_sweep_stuck_waves", MagicMock())
    monkeypatch.setattr(manager, "_sweep_digest_holds", MagicMock())
    monkeypatch.setattr(manager, "_sweep_conversations", MagicMock())
    monkeypatch.setattr(
        asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError]),
    )
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "run_in_executor",
        AsyncMock(return_value=None),
    )

    with pytest.raises(asyncio.CancelledError):
        await manager._reaper_loop()

    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED


@pytest.mark.asyncio
async def test_cancel_all_stops_startup_outbox_reconciliation(monkeypatch) -> None:
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def wait_for_delivery() -> None:
        started.set()
        await blocked.wait()

    reconcile_method = (
        "_reconcile_startup" if hasattr(manager, "_reconcile_startup") else "_reconcile_orphans"
    )
    monkeypatch.setattr(manager, reconcile_method, wait_for_delivery)
    manager.start_reaper()
    await started.wait()
    reconcile = manager._reconcile_task

    try:
        await manager.cancel_all()
        assert reconcile.done()
    finally:
        if not reconcile.done():
            reconcile.cancel()
            await asyncio.gather(reconcile, return_exceptions=True)


@pytest.mark.asyncio
async def test_adapter_acknowledges_only_after_destination_accepts() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    seen: list[str] = []

    async def deliver(claimed) -> bool:
        seen.append(claimed.event_id)
        assert claimed.status is DeliveryState.CLAIMED
        return True

    adapter = OutboxDeliveryAdapter(coordinator, deliver, owner_id="gateway", clock=clock)
    attempts = await adapter.drain_once(event_id=event.event_id)

    assert seen == [event.event_id]
    assert [attempt.status for attempt in attempts] == [DeliveryState.DELIVERED]
    assert (
        await coordinator.claim_outbox(
            OwnerLease("other", clock.value + 30), 1, event_id=event.event_id
        )
        == []
    )


@pytest.mark.asyncio
async def test_adapter_releases_failed_delivery_with_stable_event_id() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    calls = 0

    async def deliver(claimed) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("destination unavailable")
        return True

    adapter = OutboxDeliveryAdapter(
        coordinator,
        deliver,
        owner_id="gateway",
        clock=clock,
        retry_base_seconds=5.0,
    )
    failed = await adapter.drain_once(event_id=event.event_id)
    assert failed[0].status is DeliveryState.PENDING
    assert failed[0].event_id == event.event_id

    assert await adapter.drain_once(event_id=event.event_id) == []
    clock.value += 5
    delivered = await adapter.drain_once(event_id=event.event_id)

    assert delivered[0].status is DeliveryState.DELIVERED
    assert delivered[0].event_id == event.event_id
    assert calls == 2


@pytest.mark.asyncio
async def test_accepted_delivery_retries_transient_ack_without_redelivery() -> None:
    class FailOnceAckCoordinator(MemoryRunCoordinator):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.ack_calls = 0

        async def mark_delivered(self, fence):
            self.ack_calls += 1
            if self.ack_calls == 1:
                raise OSError("transient coordinator failure")
            return await super().mark_delivered(fence)

    clock = _Clock()
    coordinator = FailOnceAckCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    destination = AsyncMock(return_value=True)
    adapter = OutboxDeliveryAdapter(
        coordinator,
        destination,
        owner_id="gateway",
        clock=clock,
        retry_base_seconds=0.0,
    )

    attempts = await adapter.drain_once(event_id=event.event_id)

    assert attempts == [DeliveryAttempt(event.event_id, DeliveryState.DELIVERED)]
    destination.assert_awaited_once()
    assert destination.await_args.args[0].event_id == event.event_id
    assert coordinator.ack_calls == 2


@pytest.mark.asyncio
async def test_accepted_delivery_reclaims_expired_ack_without_redelivery() -> None:
    class AckOutageCoordinator(MemoryRunCoordinator):
        def __init__(self, *, clock: _Clock) -> None:
            super().__init__(clock=clock, id_factory=lambda: "event-1")
            self.clock = clock
            self.ack_calls = 0

        async def mark_delivered(self, fence):
            self.ack_calls += 1
            if self.ack_calls == 1:
                self.clock.value += 2_000
                raise OSError("coordinator unavailable past claim expiry")
            return await super().mark_delivered(fence)

    clock = _Clock()
    coordinator = AckOutageCoordinator(clock=clock)
    event = await _completed_event(coordinator, clock, "run-1")
    destination = AsyncMock(return_value=True)
    adapter = OutboxDeliveryAdapter(
        coordinator,
        destination,
        owner_id="gateway",
        clock=clock,
        retry_base_seconds=0.0,
    )

    attempts = await adapter.drain_once(event_id=event.event_id)
    if not attempts:
        attempts = await adapter.drain_once(event_id=event.event_id)

    assert attempts == [DeliveryAttempt(event.event_id, DeliveryState.DELIVERED)]
    destination.assert_awaited_once()
    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED


def test_retry_backoff_caps_before_large_exponent_overflows() -> None:
    clock = _Clock()
    adapter = OutboxDeliveryAdapter(
        MemoryRunCoordinator(clock=clock),
        AsyncMock(return_value=True),
        owner_id="gateway",
        clock=clock,
    )

    assert adapter._retry_at(1025) == clock.value + 300.0


@pytest.mark.asyncio
async def test_deferred_delivery_can_be_acknowledged_without_redelivery() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    calls = 0

    async def defer(_claimed) -> bool:
        nonlocal calls
        calls += 1
        return False

    adapter = OutboxDeliveryAdapter(coordinator, defer, owner_id="gateway", clock=clock)
    deferred = await adapter.drain_once(event_id=event.event_id)
    redelivered = await adapter.drain_once()
    acknowledged = await adapter.acknowledge(event.event_id)

    assert deferred[0].status is DeliveryState.PENDING
    assert redelivered == []
    assert acknowledged is not None
    assert acknowledged.status is DeliveryState.DELIVERED
    assert calls == 1


@pytest.mark.asyncio
async def test_deferred_release_failure_retains_fence_for_acknowledgement() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")

    async def defer(_claimed) -> bool:
        return False

    async def fail_release(*_args, **_kwargs):
        raise OSError("coordinator release failed")

    coordinator.release_outbox = fail_release  # type: ignore[method-assign]
    adapter = OutboxDeliveryAdapter(coordinator, defer, owner_id="gateway", clock=clock)

    with pytest.raises(OSError, match="coordinator release failed"):
        await adapter.drain_once(event_id=event.event_id)
    acknowledged = await adapter.acknowledge(event.event_id)

    assert acknowledged is not None
    assert acknowledged.status is DeliveryState.DELIVERED
    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED


@pytest.mark.asyncio
async def test_destination_can_acknowledge_without_deadlocking_adapter() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    adapter: OutboxDeliveryAdapter

    async def defer_and_ack(claimed) -> bool:
        acknowledged = await adapter.acknowledge(claimed.event_id)
        assert acknowledged is not None
        return False

    adapter = OutboxDeliveryAdapter(
        coordinator,
        defer_and_ack,
        owner_id="gateway",
        clock=clock,
    )

    attempts = await asyncio.wait_for(adapter.drain_once(event_id=event.event_id), timeout=1)

    assert [attempt.status for attempt in attempts] == [DeliveryState.DELIVERED]
    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED


@pytest.mark.asyncio
async def test_deferred_acknowledgement_reclaims_after_release_race() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    destination_started = asyncio.Event()
    release_destination = asyncio.Event()
    acknowledgement_started = asyncio.Event()
    continue_acknowledgement = asyncio.Event()
    original_mark_delivered = coordinator.mark_delivered

    async def defer(_claimed) -> bool:
        destination_started.set()
        await release_destination.wait()
        return False

    async def blocked_mark_delivered(fence):
        acknowledgement_started.set()
        await continue_acknowledgement.wait()
        return await original_mark_delivered(fence)

    coordinator.mark_delivered = blocked_mark_delivered  # type: ignore[method-assign]
    adapter = OutboxDeliveryAdapter(coordinator, defer, owner_id="gateway", clock=clock)
    drain_task = asyncio.create_task(adapter.drain_once(event_id=event.event_id))
    await destination_started.wait()
    acknowledgement_task = asyncio.create_task(adapter.acknowledge(event.event_id))
    await acknowledgement_started.wait()

    release_destination.set()
    await drain_task
    assert coordinator._outbox[event.event_id].status is DeliveryState.PENDING

    continue_acknowledgement.set()
    acknowledged = await acknowledgement_task

    assert acknowledged is not None
    assert acknowledged.status is DeliveryState.DELIVERED
    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED


@pytest.mark.asyncio
async def test_default_delivery_lease_covers_the_destination_timeout_budget() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    seen_expiry = 0.0

    async def deliver(claimed) -> bool:
        nonlocal seen_expiry
        seen_expiry = claimed.claim_expires_at
        return True

    adapter = OutboxDeliveryAdapter(coordinator, deliver, owner_id="gateway", clock=clock)
    await adapter.drain_once(event_id=event.event_id)

    assert seen_expiry - clock.value >= 1260


@pytest.mark.asyncio
async def test_each_event_gets_a_fresh_delivery_lease_when_draining_a_batch() -> None:
    clock = _Clock()
    ids = iter(("event-1", "event-2"))
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: next(ids))
    await _completed_event(coordinator, clock, "run-1")
    await _completed_event(coordinator, clock, "run-2")
    remaining: list[float] = []

    async def deliver(claimed) -> bool:
        remaining.append(claimed.claim_expires_at - clock.value)
        if len(remaining) == 1:
            clock.value += 1300
        return True

    adapter = OutboxDeliveryAdapter(coordinator, deliver, owner_id="gateway", clock=clock)
    await adapter.drain_once(limit=2)

    assert remaining == [1320.0, 1320.0]


@pytest.mark.asyncio
async def test_deferred_failed_event_acks_without_legacy_delivered_tombstone(
    monkeypatch,
) -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    marked: list[str] = []
    monkeypatch.setattr("kiro_crew.subagent.mark_delivered", marked.append)

    async def defer(info: SubagentInfo) -> None:
        info.error = "failed"
        info._delivery_queued = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=defer,
        coordinator=coordinator,
    )

    pending = await manager._outbox_delivery.drain_once(event_id=event.event_id)
    assert pending[0].status is DeliveryState.PENDING
    assert manager._delivery_event_for_run(event.run_id) == event.event_id

    await manager.settle_queued_delivery([event.run_id])

    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED
    assert marked == []


@pytest.mark.asyncio
async def test_consumed_event_retries_transient_acknowledgement_failure(monkeypatch) -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")

    async def defer(info: SubagentInfo) -> None:
        info._delivery_queued = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=defer,
        coordinator=coordinator,
    )
    await manager._outbox_delivery.drain_once(event_id=event.event_id)
    acknowledge = manager._outbox_delivery.acknowledge
    attempts = 0

    async def fail_once(event_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("coordinator unavailable")
        return await acknowledge(event_id)

    monkeypatch.setattr("kiro_crew.subagent._TERMINAL_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(manager._outbox_delivery, "acknowledge", fail_once)

    await manager.settle_queued_delivery([event.run_id])

    assert attempts == 2
    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED


@pytest.mark.asyncio
async def test_digest_held_failed_event_acks_without_legacy_delivered_tombstone(
    monkeypatch,
) -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    marked: list[str] = []
    monkeypatch.setattr("kiro_crew.subagent.mark_delivered", marked.append)

    async def hold(info: SubagentInfo) -> None:
        info.error = "failed"
        info._digest_held = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=hold,
        coordinator=coordinator,
    )
    pending = await manager._outbox_delivery.drain_once(event_id=event.event_id)
    assert pending[0].status is DeliveryState.PENDING

    flusher = SubagentInfo(id="flush", task="flush", done=True)
    flusher._digest_settle_ids = [event.run_id]
    await manager._settle_digest_holds(flusher)

    assert coordinator._outbox[event.event_id].status is DeliveryState.DELIVERED
    assert marked == []


@pytest.mark.asyncio
async def test_deferred_manager_event_does_not_repeat_parent_callbacks() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    event = await _completed_event(coordinator, clock, "run-1")
    calls = 0

    async def defer(info: SubagentInfo) -> None:
        nonlocal calls
        calls += 1
        info._delivery_queued = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=defer,
        coordinator=coordinator,
    )

    await manager._outbox_delivery.drain_once(event_id=event.event_id)
    await manager._outbox_delivery.drain_once(event_id=event.event_id)

    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_coordinator_queue_entry_commits_stopped_outcome() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="run-1",
            command_id="command-1",
            idempotency_key="key-1",
            payload_hash="hash-1",
            payload_json="{}",
            parent_session="dashboard:parent",
            agent="reviewer",
            task="inspect",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        "command-1",
        OwnerLease("executor", clock.value + 90),
    )
    assert claim is not None and claim.run is not None and claim.fence is not None
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
        coordinator=coordinator,
    )
    manager._queue = [
        {
            "task": "inspect",
            "agent": "reviewer",
            "parent_session_key": "dashboard:parent",
            "batch_id": "batch-1",
            "batch_total": 2,
            "_preassigned_id": "run-1",
            "_coordinator_admitted": True,
            "_coordinator_command": claim.command,
            "_coordinator_fence": claim.fence,
            "_coordinator_version": claim.run.version,
        }
    ]
    manager.command_authority._waiting_executions["run-1"] = (
        claim.command_fence,
        "",
    )
    manager._settle_digest_holds = AsyncMock()  # type: ignore[method-assign]

    assert await manager.cancel("run-1") is True

    run = await coordinator.get_run("run-1")
    assert run is not None
    assert run.observed_state.value == "terminal"
    assert run.outcome is RunOutcome.STOPPED
    receipt = await coordinator.get_command_by_key("key-1")
    assert receipt is not None
    assert receipt.command.status.value == "rejected"
    on_done.assert_awaited_once()
    manager._settle_digest_holds.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_queue_drain_failure_rejects_command_and_delivers_live_batch(raises: bool) -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="run-1",
            command_id="command-1",
            idempotency_key="key-1",
            payload_hash="hash-1",
            payload_json="{}",
            parent_session="dashboard:parent",
            agent="reviewer",
            task="inspect",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        "command-1",
        OwnerLease("executor", clock.value + 90),
    )
    assert claim is not None and claim.run is not None and claim.fence is not None
    delivered: list[SubagentInfo] = []

    async def on_done(info: SubagentInfo) -> None:
        delivered.append(info)

    def policy_failure() -> bool:
        raise RuntimeError("policy unavailable")

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
        max_concurrent=1,
        is_yolo=policy_failure if raises else None,
        coordinator=coordinator,
    )
    manager._spawn_stagger_secs = 0
    params = {
        "task": "inspect",
        "agent": "",
        "parent_session_key": "dashboard:parent",
        "batch_id": "batch-1",
        "batch_total": 2,
        "_preassigned_id": "run-1",
        "_coordinator_admitted": True,
        "_coordinator_command": claim.command,
        "_coordinator_fence": claim.fence,
        "_coordinator_version": claim.run.version,
    }
    manager._queue = [params]
    manager.command_authority._waiting_executions["run-1"] = (
        claim.command_fence,
        "",
    )
    if not raises:
        manager.spawn = MagicMock(
            return_value=SubagentInfo(
                id="run-1",
                task="inspect",
                parent_session_key="dashboard:parent",
                agent="reviewer",
                done=True,
                error="agent unavailable",
                batch_id="batch-1",
                batch_total=2,
            )
        )

    manager._drain_queue()
    await manager._tasks["reject-run-1"]

    receipt = await coordinator.get_command_by_key("key-1")
    run = await coordinator.get_run("run-1")
    assert receipt is not None
    assert receipt.command.status.value == "rejected"
    assert receipt.command.result_json
    assert run is not None
    assert run.outcome is RunOutcome.FAILED
    assert manager.running_count == 0
    assert manager.get("run-1") is None
    assert len(delivered) == 1
    assert delivered[0].batch_id == "batch-1"
    assert delivered[0].batch_total == 2


@pytest.mark.asyncio
async def test_terminal_context_precedes_overlapping_outbox_drain() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="run-1",
            command_id="command-1",
            idempotency_key="key-1",
            payload_hash="hash-1",
            payload_json="{}",
            parent_session="dashboard:parent",
            agent="reviewer",
            task="inspect",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        "command-1",
        OwnerLease("executor", clock.value + 90),
    )
    assert claim is not None and claim.run is not None and claim.fence is not None
    delivered: list[SubagentInfo] = []

    async def on_done(info: SubagentInfo) -> None:
        delivered.append(info)

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-1",
        task="inspect",
        parent_session_key="dashboard:parent",
        agent="reviewer",
        done=True,
        error="agent unavailable",
        batch_id="batch-1",
        batch_total=2,
    )
    info._coordinator_fence = claim.fence
    info._coordinator_version = claim.run.version

    async def overlapping_drain(_run_id: str) -> None:
        assert "event-1" in manager._outbox_contexts
        await manager._outbox_delivery.drain_once(event_id="event-1")

    manager._stop_coordinator_heartbeat = AsyncMock(  # type: ignore[method-assign]
        side_effect=overlapping_drain
    )
    manager.command_authority.stop_execution_heartbeat = AsyncMock()

    await manager._report_terminal(
        info,
        source="test",
        injection_timeout_reason="timeout",
        mark_delivered_on_success=True,
        settle_digest=True,
    )

    assert len(delivered) == 1
    assert delivered[0].batch_id == "batch-1"
    assert delivered[0].batch_total == 2


@pytest.mark.asyncio
async def test_terminal_context_precedes_commit_return_outbox_drain() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="run-1",
            command_id="command-1",
            idempotency_key="key-1",
            payload_hash="hash-1",
            payload_json="{}",
            parent_session="dashboard:parent",
            agent="reviewer",
            task="inspect",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        "command-1",
        OwnerLease("executor", clock.value + 90),
    )
    assert claim is not None and claim.run is not None and claim.fence is not None
    delivered: list[SubagentInfo] = []

    async def on_done(info: SubagentInfo) -> None:
        assert info._delivery_event_id == "event-1"
        info._delivery_queued = True
        delivered.append(info)

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-1",
        task="inspect",
        parent_session_key="dashboard:parent",
        agent="reviewer",
        done=True,
        error="agent unavailable",
        batch_id="batch-1",
        batch_total=2,
    )
    info._coordinator_fence = claim.fence
    info._coordinator_version = claim.run.version
    original_complete = coordinator.complete
    overlapping_drain_done = asyncio.Event()

    async def complete_with_overlapping_drain(completion, fence, expected_version):
        result = await original_complete(completion, fence, expected_version)
        assert result.value is not None
        await manager._outbox_delivery.drain_once(event_id=result.value.event_id)
        overlapping_drain_done.set()
        return result

    coordinator.complete = complete_with_overlapping_drain  # type: ignore[method-assign]
    report = asyncio.create_task(
        manager._report_terminal(
            info,
            source="test",
            injection_timeout_reason="timeout",
            mark_delivered_on_success=True,
            settle_digest=True,
        )
    )
    try:
        await overlapping_drain_done.wait()
        await asyncio.sleep(0)
        assert report.done()
        await report
    finally:
        if not report.done():
            report.cancel()
            await asyncio.gather(report, return_exceptions=True)

    assert len(delivered) == 1
    assert delivered[0].batch_id == "batch-1"
    assert delivered[0].batch_total == 2
    assert "event-1" in manager._outbox_contexts
    await manager._ack_delivery_for_run("run-1")
    assert manager._outbox_contexts == {}


@pytest.mark.asyncio
async def test_legacy_queue_policy_failure_announces_live_batch() -> None:
    delivered: list[SubagentInfo] = []

    async def on_done(info: SubagentInfo) -> None:
        delivered.append(info)

    def policy_failure() -> bool:
        raise RuntimeError("policy unavailable")

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
        max_concurrent=1,
        is_yolo=policy_failure,
        coordinator=MemoryRunCoordinator(),
    )
    manager._spawn_stagger_secs = 0
    manager._queue = [
        {
            "task": "inspect",
            "agent": "",
            "parent_session_key": "dashboard:parent",
            "batch_id": "batch-1",
            "batch_total": 2,
            "_preassigned_id": "legacy-run-1",
        }
    ]

    manager._drain_queue()
    await manager._tasks["reject-legacy-run-1"]

    assert manager.running_count == 0
    assert manager.get("legacy-run-1") is None
    assert len(delivered) == 1
    assert delivered[0].error == "policy unavailable"
    assert delivered[0].batch_id == "batch-1"
    assert delivered[0].batch_total == 2


@pytest.mark.asyncio
async def test_approval_rejection_requests_digest_settlement() -> None:
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=AsyncMock(),
        on_spawn_approval=AsyncMock(return_value=False),
        coordinator=MemoryRunCoordinator(),
    )
    info = SubagentInfo(
        id="run-1",
        task="inspect",
        parent_session_key="dashboard:parent",
        batch_id="batch-1",
        batch_total=2,
    )
    info._coordinator_fence = RunFence("run-1", "executor", 1)
    authority_stop = AsyncMock()
    authority_reject = AsyncMock()
    manager.command_authority.stop_execution_heartbeat = authority_stop
    manager.command_authority.reject_waiting_execution = authority_reject

    async def report_while_lease_is_live(*_args, **_kwargs) -> None:
        authority_stop.assert_not_awaited()

    manager._report_terminal = AsyncMock(  # type: ignore[method-assign]
        side_effect=report_while_lease_is_live
    )

    await manager._spawn_with_approval(info)

    authority_reject.assert_awaited_once_with(
        info.id,
        "spawn rejected",
        stop_heartbeat=False,
    )
    assert manager._report_terminal.await_args.kwargs["settle_digest"] is True


@pytest.mark.asyncio
async def test_exact_outbox_claim_does_not_consume_older_event() -> None:
    clock = _Clock()
    ids = iter(("event-1", "event-2"))
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: next(ids))
    first = await _completed_event(coordinator, clock, "run-1")
    clock.value += 1
    second = await _completed_event(coordinator, clock, "run-2")

    claimed = await coordinator.claim_outbox(
        OwnerLease("gateway", clock.value + 30),
        1,
        event_id=second.event_id,
    )
    remaining = await coordinator.claim_outbox(
        OwnerLease("other", clock.value + 30),
        1,
        event_id=first.event_id,
    )

    assert [item.event_id for item in claimed] == [second.event_id]
    assert [item.event_id for item in remaining] == [first.event_id]


@pytest.mark.asyncio
async def test_manager_commits_terminal_before_callbacks_and_bounds_payload() -> None:
    clock = _Clock()
    coordinator = MemoryRunCoordinator(clock=clock, id_factory=lambda: "event-1")
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="run-1",
            command_id="command-1",
            idempotency_key="key-1",
            payload_hash="hash-1",
            payload_json="{}",
            parent_session="dashboard:parent",
            agent="reviewer",
            task="inspect",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        "command-1",
        OwnerLease("executor", clock.value + 90),
    )
    assert claim is not None and claim.run is not None and claim.fence is not None

    callback_states: list[str] = []

    async def on_done(info: SubagentInfo) -> None:
        run = await coordinator.get_run(info.id)
        assert run is not None
        callback_states.append(run.observed_state.value)
        assert info._delivery_event_id == "event-1"

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-1",
        task="inspect",
        parent_session_key="dashboard:parent",
        agent="reviewer",
        done=True,
        result="x" * 20_000,
        result_path="/results/run-1.txt",
        silent=True,
        batch_id="batch-1",
        batch_total=2,
    )
    info._coordinator_admitted = True
    info._coordinator_command = claim.command
    info._coordinator_fence = claim.fence
    info._coordinator_version = claim.run.version
    await manager._coordinator_mark_starting(info)
    await manager._coordinator_mark_running(info)

    await manager._report_terminal(
        info,
        source="test",
        injection_timeout_reason="timeout",
        mark_delivered_on_success=False,
    )

    assert callback_states == ["terminal"]
    event = coordinator._outbox["event-1"]
    payload = json.loads(event.payload_json)
    assert event.status is DeliveryState.DELIVERED
    assert payload["result_path"] == "/results/run-1.txt"
    assert len(payload["result_summary"]) == 4000
    assert payload["result_truncated"] is True
    assert payload["silent"] is True
    assert payload["batch_id"] == "batch-1"
    assert payload["batch_total"] == 2
    recovered = manager._info_from_outbox(event)
    assert recovered.silent is True
    assert recovered.batch_id == ""
    assert recovered.batch_total == 0


@pytest.mark.asyncio
async def test_run_advances_fenced_lifecycle_and_stops_heartbeat() -> None:
    coordinator = MemoryRunCoordinator(id_factory=lambda: "event-1")
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="run-1",
            command_id="command-1",
            idempotency_key="key-1",
            payload_hash="hash-1",
            payload_json="{}",
            parent_session="dashboard:parent",
            agent="reviewer",
            task="inspect",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        "command-1",
        OwnerLease("executor", 10**12),
    )
    assert claim is not None and claim.run is not None and claim.fence is not None

    delivered: list[str] = []

    async def on_done(info: SubagentInfo) -> None:
        delivered.append(info.id)

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-1",
        task="inspect",
        parent_session_key="dashboard:parent",
    )
    info._coordinator_admitted = True
    info._coordinator_command = claim.command
    info._coordinator_fence = claim.fence
    info._coordinator_version = claim.run.version

    async def run_inner(target: SubagentInfo, _session_key: str) -> None:
        await manager._coordinator_mark_running(target)
        target.result = "done"
        target.done = True

    manager._run_inner = run_inner  # type: ignore[method-assign]
    manager._teardown_run_session = AsyncMock()  # type: ignore[method-assign]
    manager._record_cost = MagicMock()  # type: ignore[method-assign]
    manager._scheduler.occupy(info, 0.0)

    await manager._run(info)

    run = await coordinator.get_run("run-1")
    assert run is not None
    assert run.observed_state.value == "terminal"
    assert run.outcome is RunOutcome.COMPLETED
    assert delivered == ["run-1"]
    assert manager._lease_tasks == {}
