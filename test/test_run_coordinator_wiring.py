"""The typed coordinator seam is injectable before it becomes authoritative."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.run_coordinator import (
    CommandOperation,
    CoordinatorDecision,
    MemoryRunCoordinator,
    OwnerLease,
    SQLiteRunCoordinator,
    SubmitRun,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_command_authority import AuthorityOutcomeUncertain


def test_subagent_manager_defaults_to_durable_sqlite_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kiro_crew.subagent.SQLiteRunCoordinator",
        SQLiteRunCoordinator,
    )
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    assert isinstance(manager._coordinator, SQLiteRunCoordinator)


def test_subagent_manager_preserves_injected_coordinator_identity() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    assert manager._coordinator is coordinator
    assert manager.command_authority._coordinator is coordinator
    assert manager.command_authority._manager is manager


@pytest.mark.asyncio
async def test_accepted_run_is_submitted_after_legacy_admission() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-1",
        task="redacted task",
        parent_session_key="parent-1",
        agent="researcher",
        model="served-model",
        reasoning_effort="high",
        allowed_tools=["Read"],
        bare=True,
        cwd="/tmp/project",
        silent=True,
        max_turns=7,
        keep=True,
        include_memory=False,
        include_lessons=True,
        include_project=False,
    )
    info._raw_task = "raw task"

    await manager._shadow_submit_accepted_run(info)

    run = await coordinator.get_run("run-1")
    assert run is not None
    assert run.task == "raw task"
    assert info._coordinator_fence is not None
    command = info._coordinator_command
    assert command is not None
    assert command.operation is CommandOperation.SPAWN
    payload = json.loads(command.payload_json)
    assert payload == {
        "agent": "researcher",
        "allowed_tools": ["Read"],
        "app": "",
        "bare": True,
        "batch_id": "",
        "batch_total": 0,
        "conversation_key": "",
        "cwd": "/tmp/project",
        "include_lessons": True,
        "include_memory": False,
        "include_project": False,
        "keep": True,
        "max_turns": 7,
        "model": "served-model",
        "operation": "spawn",
        "parent_session": "parent-1",
        "reasoning_effort": "high",
        "run_id": "run-1",
        "silent": True,
        "task": "raw task",
    }


@pytest.mark.asyncio
async def test_default_shadow_persists_accepted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kiro_crew.subagent.SQLiteRunCoordinator",
        SQLiteRunCoordinator,
    )
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

    await manager._shadow_submit_accepted_run(SubagentInfo(id="durable-run", task="task"))

    stored = await SQLiteRunCoordinator(tmp_path / "run-coordinator/coordinator.db").get_run(
        "durable-run"
    )
    assert stored is not None
    assert stored.task == "task"


@pytest.mark.asyncio
async def test_manager_persists_process_identity_through_its_execution_fence() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    await coordinator.submit(
        SubmitRun(
            run_id="process-run",
            command_id="process-command",
            idempotency_key="process-key",
            payload_hash="process-hash",
            parent_session="parent-1",
            agent="researcher",
            task="task",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    claim = await coordinator.claim_command(
        "process-command",
        OwnerLease("executor", 9_999_999_999.0),
    )
    assert claim is not None
    info = SubagentInfo(id="process-run", task="task")
    info._coordinator_command = claim.command
    info._coordinator_fence = claim.fence
    info._coordinator_version = claim.run.version

    await manager._coordinator_mark_starting(info)
    await manager._coordinator_mark_running(info)
    await manager._coordinator_record_process(info, 4321, "start-1", True)

    stored = await coordinator.get_run("process-run")
    assert stored is not None
    assert stored.process_id == 4321
    assert stored.process_start_id == "start-1"
    assert stored.process_owned is True
    assert info._coordinator_version == stored.version


@pytest.mark.asyncio
async def test_continuation_submission_is_stable_and_idempotent() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-2",
        task="follow up",
        parent_session_key="parent-1",
        conversation_key="subagent:original",
    )
    info._raw_task = "follow up"

    await manager._shadow_submit_accepted_run(info)
    await manager._shadow_submit_accepted_run(info)

    receipt = await coordinator.get_command_by_key("continue:run-2")
    assert receipt is not None
    assert receipt.command.operation is CommandOperation.CONTINUE
    assert receipt.command.command_id == "continue:run-2"
    assert info._coordinator_fence is not None


@pytest.mark.asyncio
async def test_shadow_submission_failure_preserves_legacy_execution() -> None:
    coordinator = AsyncMock()
    coordinator.submit.side_effect = RuntimeError("database unavailable")
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="run-3", task="task")

    await manager._shadow_submit_accepted_run(info)

    coordinator.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_shadow_request_construction_failure_is_contained() -> None:
    coordinator = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="run-invalid", task="task")
    info.allowed_tools = [{"not-json-serializable"}]  # type: ignore[list-item]

    await manager._shadow_submit_accepted_run(info)

    coordinator.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_starting_transition_retries_a_lost_commit_response() -> None:
    coordinator = AsyncMock()
    committed = MagicMock()
    committed.decision = CoordinatorDecision.UNCHANGED
    committed.value.version = 4
    coordinator.mark_starting.side_effect = [OSError("response lost"), committed]
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="starting-response-lost", task="task")
    info._coordinator_command = MagicMock()
    info._coordinator_fence = MagicMock()
    info._coordinator_version = 3

    await manager._coordinator_mark_starting(info)

    assert coordinator.mark_starting.await_count == 2
    assert info._coordinator_version == 4
    assert info._coordinator_started is True


@pytest.mark.asyncio
async def test_running_transition_retries_a_lost_commit_response() -> None:
    coordinator = AsyncMock()
    committed = MagicMock()
    committed.decision = CoordinatorDecision.UNCHANGED
    committed.value.version = 5
    coordinator.mark_running.side_effect = [OSError("response lost"), committed]
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="running-response-lost", task="task")
    info._coordinator_fence = MagicMock()
    info._coordinator_version = 4

    await manager._coordinator_mark_running(info)

    assert coordinator.mark_running.await_count == 2
    assert info._coordinator_version == 5
    assert info._coordinator_running is True


@pytest.mark.asyncio
async def test_unfenced_shadow_submission_aborts_before_execution() -> None:
    order: list[str] = []
    coordinator = AsyncMock()

    async def submit(_request: object) -> object:
        order.append("submit")
        return None

    coordinator.submit.side_effect = submit
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )

    async def run_inner(_info: SubagentInfo, _session_key: str) -> None:
        order.append("execute")

    manager._run_inner = AsyncMock(side_effect=run_inner)
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)

    info = SubagentInfo(id="run-4", task="task")
    await manager._run(info)

    assert order == ["submit"]
    assert info.done is True
    assert "coordinator execution fence is missing" in info.error
    manager._run_inner.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_shadow_submit_defers_terminal_delivery_to_recovery() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            self.now = time.time()
            super().__init__(clock=lambda: self.now)
            self.release_submit = asyncio.Event()
            self.submit_cancelled = False

        async def submit(self, request):
            try:
                await self.release_submit.wait()
            except asyncio.CancelledError:
                self.submit_cancelled = True
                raise
            return await super().submit(request)

    coordinator = DelayedSubmitCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(id="late-submit", task="task")
    manager._agents[info.id] = info

    real_wait_for = asyncio.wait_for
    wait_calls = 0

    async def deterministic_timeout(awaitable, *, timeout):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            outer = asyncio.ensure_future(awaitable)
            await asyncio.sleep(0)
            outer.cancel()
            await asyncio.gather(outer, return_exceptions=True)
            raise asyncio.TimeoutError
        return await real_wait_for(awaitable, timeout=timeout)

    with patch("kiro_crew.subagent.asyncio.wait_for", side_effect=deterministic_timeout):
        await manager._run(info)

    assert info._coordinator_claim_uncertain is True
    assert coordinator.submit_cancelled is False
    assert len(manager._coordinator_shadow_submits) == 1
    manager._run_inner.assert_not_awaited()
    manager._fire_event.assert_not_awaited()
    on_done.assert_not_awaited()

    coordinator.release_submit.set()
    await asyncio.gather(*manager._coordinator_shadow_submits)
    assert manager._coordinator_shadow_submits == set()
    coordinator.now += 1_000
    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", coordinator.now + 100),
        limit=1,
    )
    assert len(claims) == 1
    completed = await coordinator.complete(
        manager._run_recovery.completion_for(claims[0].run),
        claims[0].fence,
        claims[0].run.version,
    )
    assert completed.value is not None
    await manager._outbox_delivery.drain_once()

    manager._fire_event.assert_awaited_once()
    on_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_timed_out_shadow_submit_late_failure_resumes_legacy_delivery() -> None:
    class FailingDelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_submit = asyncio.Event()

        async def submit(self, request):
            await self.release_submit.wait()
            raise RuntimeError("sqlite write failed")

    coordinator = FailingDelayedSubmitCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(id="failed-late-submit", task="task")
    manager._agents[info.id] = info

    real_wait_for = asyncio.wait_for
    wait_calls = 0

    async def deterministic_timeout(awaitable, *, timeout):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            outer = asyncio.ensure_future(awaitable)
            await asyncio.sleep(0)
            outer.cancel()
            await asyncio.gather(outer, return_exceptions=True)
            raise asyncio.TimeoutError
        return await real_wait_for(awaitable, timeout=timeout)

    with patch("kiro_crew.subagent.asyncio.wait_for", side_effect=deterministic_timeout):
        await manager._run(info)

    assert info._coordinator_claim_uncertain is True
    on_done.assert_not_awaited()

    coordinator.release_submit.set()
    while manager._coordinator_shadow_submits:
        await asyncio.gather(*manager._coordinator_shadow_submits, return_exceptions=True)
        await asyncio.sleep(0)
    if manager._report_tasks:
        await asyncio.gather(*manager._report_tasks)

    assert info._coordinator_claim_uncertain is False
    assert info._finalized is True
    manager._fire_event.assert_awaited_once()
    on_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_shadow_submit_failure_writes_tombstone_off_loop() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="late-shadow-failure", task="task")
    info._coordinator_claim_uncertain = True
    manager._claim_finalize = MagicMock(return_value=True)
    manager._record_cost = MagicMock()
    manager._spawn_terminal_report = MagicMock()
    tombstone_threads: list[int] = []
    manager._write_tombstone = MagicMock(
        side_effect=lambda *_args: tombstone_threads.append(threading.get_ident())
    )

    settlement = manager._resume_legacy_terminal_after_failed_shadow_submit(info)
    if inspect.isawaitable(settlement):
        await settlement

    assert tombstone_threads
    assert tombstone_threads != [threading.get_ident()]


@pytest.mark.asyncio
async def test_timed_out_shadow_claim_defers_terminal_delivery_to_recovery() -> None:
    class DelayedClaimCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            self.now = time.time()
            super().__init__(clock=lambda: self.now)
            self.release_claim = asyncio.Event()
            self.lookup_started = asyncio.Event()
            self.claim_task: asyncio.Task | None = None

        async def claim_command(self, command_id, owner):
            async def commit_claim():
                await self.release_claim.wait()
                return await super(DelayedClaimCoordinator, self).claim_command(
                    command_id,
                    owner,
                )

            self.claim_task = asyncio.create_task(commit_claim())
            raise asyncio.TimeoutError

        async def get_command_by_key(self, idempotency_key):
            self.lookup_started.set()
            return await super().get_command_by_key(idempotency_key)

    coordinator = DelayedClaimCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(
        id="late-claim",
        task="task",
        batch_id="batch-late",
        batch_total=2,
    )
    manager._agents[info.id] = info

    run_task = asyncio.create_task(manager._run(info))
    await asyncio.wait_for(coordinator.lookup_started.wait(), timeout=1)
    await run_task
    coordinator.release_claim.set()
    assert coordinator.claim_task is not None
    await coordinator.claim_task

    receipt = await coordinator.get_command_by_key("spawn:late-claim")
    assert receipt is not None
    assert receipt.command.status.value == "claimed"
    assert info._coordinator_claim_uncertain is True
    assert info._finalized is False
    assert manager.batch_members_pending("batch-late") is True
    manager._run_inner.assert_not_awaited()
    manager._fire_event.assert_not_awaited()
    on_done.assert_not_awaited()

    coordinator.now += 1_000
    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", coordinator.now + 100),
        limit=1,
    )
    assert len(claims) == 1
    completion = manager._run_recovery.completion_for(claims[0].run)
    completed = await coordinator.complete(
        completion,
        claims[0].fence,
        claims[0].run.version,
    )
    assert completed.value is not None
    await manager._outbox_delivery.drain_once()

    manager._fire_event.assert_awaited_once()
    on_done.assert_awaited_once()
    delivered = on_done.await_args.args[0]
    assert delivered.batch_id == "batch-late"
    assert delivered.batch_total == 2


@pytest.mark.asyncio
async def test_timed_out_shadow_claim_resumes_from_stable_committed_fence() -> None:
    class ClaimBeforeLookupCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_claim = asyncio.Event()
            self.claim_task: asyncio.Task | None = None

        async def claim_command(self, command_id, owner):
            async def commit_claim():
                await self.release_claim.wait()
                return await super(ClaimBeforeLookupCoordinator, self).claim_command(
                    command_id,
                    owner,
                )

            self.claim_task = asyncio.create_task(commit_claim())
            raise asyncio.TimeoutError

        async def get_command_by_key(self, idempotency_key):
            self.release_claim.set()
            assert self.claim_task is not None
            await self.claim_task
            return await super().get_command_by_key(idempotency_key)

    coordinator = ClaimBeforeLookupCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._start_coordinator_heartbeat = MagicMock()
    info = SubagentInfo(id="resolved-claim", task="task")

    await manager._run(info)

    assert info._coordinator_claim_uncertain is False
    assert info._coordinator_fence is not None
    assert info._coordinator_fence.owner_id == manager._coordinator_owner_id
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_authoritatively_admitted_run_does_not_submit_twice() -> None:
    coordinator = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock()
    info = SubagentInfo(id="run-5", task="task", _coordinator_admitted=True)
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    coordinator.submit.assert_not_awaited()
    manager._run_inner.assert_awaited_once()
    manager.command_authority.execution_started.assert_awaited_once_with("run-5")


@pytest.mark.asyncio
async def test_failed_start_settlement_keeps_waiting_run_retryable() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=True)
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock(
        side_effect=AuthorityOutcomeUncertain("write failed")
    )
    info = SubagentInfo(
        id="waiting-run",
        task="task",
        batch_id="waiting-wave",
        batch_total=3,
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is True
    assert info._coordinator_claim_uncertain is True
    assert info.done is False
    assert manager._outbox_live_run_batches[info.id] == ("waiting-wave", 3)
    manager._run_inner.assert_not_awaited()
    manager._claim_finalize.assert_not_called()
    assert manager.command_authority.execution_started.await_count == 2


@pytest.mark.asyncio
async def test_failed_lifecycle_start_transition_never_applies_command() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=True)
    manager._coordinator_mark_starting = AsyncMock(side_effect=OSError("write failed"))
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock()
    info = SubagentInfo(
        id="uncommitted-start",
        task="task",
        batch_id="starting-wave",
        batch_total=4,
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is True
    assert info._coordinator_claim_uncertain is True
    assert info.done is False
    assert manager._outbox_live_run_batches[info.id] == ("starting-wave", 4)
    manager.command_authority.execution_started.assert_not_awaited()
    manager._run_inner.assert_not_awaited()
    manager._claim_finalize.assert_not_called()


@pytest.mark.asyncio
async def test_lost_start_settlement_response_reconciles_before_execution() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock(
        side_effect=[AuthorityOutcomeUncertain("response lost"), None]
    )
    info = SubagentInfo(
        id="reconciled-start",
        task="task",
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is False
    assert info._coordinator_claim_uncertain is False
    assert manager.command_authority.execution_started.await_count == 2
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_cancel_keeps_authority_lease_until_terminal_commit() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    queued = {"_preassigned_id": "queued-run"}
    manager._unqueue = MagicMock(return_value=[queued])
    manager._finalize_queued_cancel = AsyncMock()
    manager.command_authority.stop_execution_heartbeat = AsyncMock()

    assert await manager.cancel("queued-run") is True

    manager.command_authority.stop_execution_heartbeat.assert_not_awaited()
    manager._finalize_queued_cancel.assert_awaited_once_with(queued)


@pytest.mark.asyncio
async def test_manager_shutdown_closes_command_authority() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()

    await manager.cancel_all()

    manager.command_authority.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_manager_shutdown_drains_retained_shadow_submission() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    async def retained_submit() -> None:
        started.set()
        await release.wait()
        settled.set()

    submit_task = asyncio.create_task(retained_submit())
    manager._coordinator_shadow_submits.add(submit_task)
    await started.wait()

    shutdown_task = asyncio.create_task(manager.cancel_all())
    await asyncio.sleep(0)

    assert submit_task.cancelled() is False
    assert shutdown_task.done() is False

    release.set()
    await shutdown_task

    assert settled.is_set()
    assert manager._coordinator_shadow_submits == set()
    manager.command_authority.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_manager_shutdown_drains_report_created_by_late_shadow_settlement() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()
    submit_release = asyncio.Event()
    report_started = asyncio.Event()
    report_release = asyncio.Event()
    info = SubagentInfo(id="late-shadow-report", task="task")

    async def retained_submit() -> None:
        await submit_release.wait()

    async def late_report() -> None:
        report_started.set()
        await report_release.wait()

    submit_task = asyncio.create_task(retained_submit())
    manager._coordinator_shadow_submits.add(submit_task)

    def schedule_report(_done: asyncio.Task[None]) -> None:
        manager._lifecycle.spawn_report(info, late_report)

    submit_task.add_done_callback(schedule_report)
    shutdown_task = asyncio.create_task(manager.cancel_all())
    await asyncio.sleep(0)
    submit_release.set()
    await report_started.wait()

    assert shutdown_task.done() is False

    report_release.set()
    await shutdown_task


@pytest.mark.asyncio
async def test_process_identity_persistence_failure_aborts_before_execution(monkeypatch) -> None:
    sessions = MagicMock()
    sessions.get_pid.return_value = 4321
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    info = SubagentInfo(id="run-process", task="inspect")
    manager._coordinator_record_process = AsyncMock(side_effect=OSError("write failed"))
    monkeypatch.setattr("kiro_crew.subagent.platform_compat.process_start_time", lambda _pid: "s1")
    monkeypatch.setattr("kiro_crew.subagent.update_state", MagicMock())

    with pytest.raises(OSError, match="write failed"):
        await manager._record_process_identity(info, "subagent:run-process")

    assert info._pid == 4321


@pytest.mark.asyncio
async def test_process_identity_requires_an_execution_fence() -> None:
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )

    with pytest.raises(RuntimeError, match="execution fence is missing"):
        await manager._coordinator_record_process(
            SubagentInfo(id="unfenced-run", task="inspect"),
            4321,
            "start-1",
            True,
        )
