"""Behavioral contract shared by durable run coordinator implementations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kiro_crew.run_coordinator import (
    CommandClaim,
    CommandFence,
    CommandOperation,
    CommandStatus,
    CoordinatorDecision,
    CoordinatorReason,
    DeliveryFence,
    DeliveryState,
    MemoryRunCoordinator,
    ObservedState,
    OwnerLease,
    RunCompletion,
    RunCoordinator,
    RunOutcome,
    RunRecord,
    SQLiteRunCoordinator,
    SubmitControl,
    SubmitRun,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=("memory", "sqlite"))
def coordinator(
    request: pytest.FixtureRequest,
    clock: FakeClock,
    tmp_path: Path,
) -> RunCoordinator:
    ids = iter(("event-1", "event-2", "event-3"))
    kwargs = {"clock": clock, "id_factory": lambda: next(ids)}
    if request.param == "sqlite":
        return SQLiteRunCoordinator(tmp_path / "coordinator.db", **kwargs)
    return MemoryRunCoordinator(**kwargs)


def _request(
    *,
    run_id: str = "run-1",
    command_id: str = "command-1",
    idempotency_key: str = "key-1",
    payload_json: str = '{"task":"compare the candidates","version":1}',
    payload_hash: str = "hash-1",
    accepted: bool = True,
    operation: CommandOperation = CommandOperation.SPAWN,
) -> SubmitRun:
    return SubmitRun(
        run_id=run_id,
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_json=payload_json,
        payload_hash=payload_hash,
        parent_session="dashboard:parent",
        agent="researcher",
        task="compare the candidates",
        conversation_key="",
        operation=operation,
        accepted=accepted,
        rejection_reason="governance denied" if not accepted else "",
    )


async def _claimed_running(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> tuple[CommandClaim, RunRecord]:
    receipt = await coordinator.submit(_request())
    assert receipt.value is not None
    claims = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 30),
        limit=1,
    )
    assert len(claims) == 1
    claim = claims[0]
    starting = await coordinator.mark_starting(
        claim.command,
        claim.fence,
        expected_version=claim.run.version,
    )
    assert starting.value is not None
    running = await coordinator.mark_running(
        claim.command.run_id,
        claim.fence,
        expected_version=starting.value.version,
    )
    assert running.value is not None
    return claim, running.value


@pytest.mark.asyncio
async def test_submit_is_idempotent_and_detects_payload_conflicts(
    coordinator: RunCoordinator,
) -> None:
    created = await coordinator.submit(_request())
    replay = await coordinator.submit(_request())
    conflict = await coordinator.submit(_request(payload_hash="different"))

    assert created.decision is CoordinatorDecision.APPLIED
    assert created.reason is CoordinatorReason.CREATED
    assert created.value is not None
    assert created.value.created is True
    assert created.value.run.run_id == "run-1"
    assert created.value.command.status is CommandStatus.PENDING
    assert created.value.command.payload_json == _request().payload_json

    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.reason is CoordinatorReason.IDEMPOTENT_REPLAY
    assert replay.value is not None
    assert replay.value.created is False
    assert replay.value.run == created.value.run
    assert replay.value.command == created.value.command

    assert conflict.decision is CoordinatorDecision.REJECTED
    assert conflict.reason is CoordinatorReason.IDEMPOTENCY_CONFLICT
    assert conflict.value is None
    assert await coordinator.get_run("run-1") == created.value.run


@pytest.mark.asyncio
async def test_rejected_submission_is_queryable_but_never_claimed(
    coordinator: RunCoordinator,
) -> None:
    result = await coordinator.submit(_request(accepted=False))

    assert result.decision is CoordinatorDecision.REJECTED
    assert result.reason is CoordinatorReason.ADMISSION_REJECTED
    assert result.value is not None
    assert result.value.run.observed_state is ObservedState.TERMINAL
    assert result.value.run.outcome is RunOutcome.FAILED
    assert result.value.command.status is CommandStatus.REJECTED
    assert (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=130.0), limit=1
        )
        == []
    )


@pytest.mark.asyncio
async def test_submit_rejects_duplicate_run_or_command_identity(
    coordinator: RunCoordinator,
) -> None:
    created = await coordinator.submit(_request())
    duplicate_run = await coordinator.submit(
        _request(command_id="command-2", idempotency_key="key-2")
    )
    duplicate_command = await coordinator.submit(_request(run_id="run-2", idempotency_key="key-3"))

    assert duplicate_run.decision is CoordinatorDecision.REJECTED
    assert duplicate_run.reason is CoordinatorReason.IDENTITY_CONFLICT
    assert duplicate_command.decision is CoordinatorDecision.REJECTED
    assert duplicate_command.reason is CoordinatorReason.IDENTITY_CONFLICT
    assert created.value is not None
    assert await coordinator.get_run("run-1") == created.value.run
    assert await coordinator.get_run("run-2") is None


@pytest.mark.asyncio
async def test_submit_rejects_control_commands_until_target_semantics_exist(
    coordinator: RunCoordinator,
) -> None:
    result = await coordinator.submit(_request(operation=CommandOperation.STEER))

    assert result.decision is CoordinatorDecision.REJECTED
    assert result.reason is CoordinatorReason.UNSUPPORTED_OPERATION
    assert result.value is None
    assert await coordinator.get_run("run-1") is None


@pytest.mark.asyncio
async def test_control_submission_is_idempotent_and_queryable_without_mutating_run(
    coordinator: RunCoordinator,
) -> None:
    created_run = await coordinator.submit(_request())
    assert created_run.value is not None
    before = created_run.value.run
    request = SubmitControl(
        command_id="control-1",
        idempotency_key="control-key-1",
        run_id="run-1",
        operation=CommandOperation.STEER,
        payload_hash="control-hash-1",
        payload_json='{"message":"focus"}',
    )

    created = await coordinator.submit_control(request)
    replay = await coordinator.submit_control(request)
    conflict = await coordinator.submit_control(replace(request, payload_hash="different"))
    queried = await coordinator.get_command_by_key("control-key-1")

    assert created.decision is CoordinatorDecision.APPLIED
    assert created.value is not None
    assert created.value.created is True
    assert created.value.command.status is CommandStatus.PENDING
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.value is not None
    assert replay.value.created is False
    assert replay.value.command == created.value.command
    assert conflict.reason is CoordinatorReason.IDEMPOTENCY_CONFLICT
    assert queried == replay.value
    assert await coordinator.get_run("run-1") == before


@pytest.mark.asyncio
async def test_rejected_control_is_sticky_never_claimed_and_does_not_terminal_run(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    created_run = await coordinator.submit(_request())
    assert created_run.value is not None
    before = created_run.value.run
    request = SubmitControl(
        command_id="control-1",
        idempotency_key="control-key-1",
        run_id="run-1",
        operation=CommandOperation.CANCEL,
        payload_hash="control-hash-1",
        accepted=False,
        rejection_reason="not authorized",
    )

    rejected = await coordinator.submit_control(request)
    replay = await coordinator.submit_control(request)

    assert rejected.reason is CoordinatorReason.ADMISSION_REJECTED
    assert rejected.value is not None
    assert rejected.value.command.status is CommandStatus.REJECTED
    assert replay.value is not None
    assert replay.value.command.status is CommandStatus.REJECTED
    assert await coordinator.get_run("run-1") == before
    assert (
        await coordinator.claim_controls(OwnerLease("controller", clock.value + 10), limit=1) == []
    )


@pytest.mark.asyncio
async def test_control_for_legacy_target_does_not_require_coordinator_run(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    submitted = await coordinator.submit_control(
        SubmitControl(
            command_id="control-legacy",
            idempotency_key="control-key-legacy",
            run_id="legacy-run",
            operation=CommandOperation.STEER,
            payload_hash="control-hash-legacy",
        )
    )
    claim = (await coordinator.claim_controls(OwnerLease("controller", clock.value + 10), limit=1))[
        0
    ]

    assert submitted.value is not None
    assert submitted.value.run is None
    assert claim.run is None
    assert claim.fence is None


@pytest.mark.asyncio
async def test_control_claim_epoch_is_independent_from_execution_lease(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    execution = (
        await coordinator.claim_commands(OwnerLease("executor", clock.value + 100), limit=1)
    )[0]
    await coordinator.submit_control(
        SubmitControl(
            command_id="control-1",
            idempotency_key="control-key-1",
            run_id="run-1",
            operation=CommandOperation.CANCEL,
            payload_hash="control-hash-1",
        )
    )
    run_before = await coordinator.get_run("run-1")

    first = (
        await coordinator.claim_controls(OwnerLease("controller-1", clock.value + 5), limit=1)
    )[0]
    assert first.fence is None
    assert first.command_fence == CommandFence("control-1", "controller-1", 1)
    assert await coordinator.get_run("run-1") == run_before

    clock.value += 6
    second = (
        await coordinator.claim_controls(OwnerLease("controller-2", clock.value + 5), limit=1)
    )[0]
    stale = await coordinator.finish_control(first.command_fence, CommandStatus.APPLIED)
    applied = await coordinator.finish_control(
        second.command_fence,
        CommandStatus.APPLIED,
        "",
        '{"cancelled":true}',
    )

    assert execution.fence is not None
    assert second.command_fence.claim_epoch == first.command_fence.claim_epoch + 1
    assert stale.reason is CoordinatorReason.STALE_FENCE
    assert applied.decision is CoordinatorDecision.APPLIED
    assert applied.value is not None
    assert applied.value.result_json == '{"cancelled":true}'
    assert await coordinator.get_run("run-1") == run_before


@pytest.mark.asyncio
async def test_control_claim_can_finish_after_deadline_until_it_is_superseded(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit_control(
        SubmitControl(
            command_id="slow-control",
            idempotency_key="slow-control-key",
            run_id="legacy-run",
            operation=CommandOperation.CANCEL,
            payload_hash="slow-control-hash",
        )
    )
    claim = (
        await coordinator.claim_controls(
            OwnerLease("controller", clock.value + 5),
            limit=1,
        )
    )[0]
    clock.value += 6

    finished = await coordinator.finish_control(
        claim.command_fence,
        CommandStatus.APPLIED,
        result_json="true",
    )

    assert finished.decision is CoordinatorDecision.APPLIED
    assert finished.value is not None
    assert finished.value.result_json == "true"


@pytest.mark.asyncio
async def test_control_claim_can_target_one_command_and_finish_rejection(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    for index, operation in enumerate((CommandOperation.STEER, CommandOperation.RELEASE), start=1):
        await coordinator.submit_control(
            SubmitControl(
                command_id=f"control-{index}",
                idempotency_key=f"control-key-{index}",
                run_id="run-1",
                operation=operation,
                payload_hash=f"control-hash-{index}",
            )
        )

    claims = await coordinator.claim_controls(
        OwnerLease("controller", clock.value + 10),
        limit=2,
        command_id="control-2",
    )
    assert [claim.command.command_id for claim in claims] == ["control-2"]
    rejected = await coordinator.finish_control(
        claims[0].command_fence,
        CommandStatus.REJECTED,
        "conversation busy",
        '{"ok":false}',
    )
    queried = await coordinator.get_command_by_key("control-key-2")

    assert rejected.decision is CoordinatorDecision.APPLIED
    assert rejected.value is not None
    assert rejected.value.status is CommandStatus.REJECTED
    assert rejected.value.rejection_reason == "conversation busy"
    assert queried is not None
    assert queried.command == rejected.value
    remaining = await coordinator.claim_controls(
        OwnerLease("controller", clock.value + 10), limit=2
    )
    assert [claim.command.command_id for claim in remaining] == ["control-1"]


@pytest.mark.asyncio
async def test_exact_execution_command_claim_and_finish_are_idempotent(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    before = await coordinator.get_run("run-1")

    claim = await coordinator.claim_command("command-1", OwnerLease("admission", clock.value + 10))
    duplicate_claim = await coordinator.claim_command(
        "command-1", OwnerLease("other", clock.value + 10)
    )
    assert claim is not None
    assert claim.fence is not None
    assert claim.run is not None
    assert claim.run.owner_id == "admission"
    assert claim.fence.lease_epoch == claim.run.lease_epoch
    assert duplicate_claim is None
    leased = await coordinator.get_run("run-1")
    assert leased is not None
    assert before is not None
    assert leased.version == before.version
    assert leased.lease_epoch == before.lease_epoch + 1

    applied = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.APPLIED,
        "",
        '{"id":"run-1","queued":false}',
    )
    replay = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.APPLIED,
        "",
        '{"id":"run-1","queued":false}',
    )
    conflicting_replay = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.REJECTED,
        "different outcome",
        '{"error":"different outcome"}',
    )
    queried = await coordinator.get_command_by_key("key-1")

    assert applied.decision is CoordinatorDecision.APPLIED
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert conflicting_replay.decision is CoordinatorDecision.REJECTED
    assert conflicting_replay.reason is CoordinatorReason.OUTCOME_CONFLICT
    assert queried is not None
    assert queried.command.status is CommandStatus.APPLIED
    assert queried.command.result_json == '{"id":"run-1","queued":false}'
    assert await coordinator.get_run("run-1") == leased


@pytest.mark.asyncio
async def test_execution_rejection_atomically_terminals_created_run(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    claim = await coordinator.claim_command("command-1", OwnerLease("admission", clock.value + 10))
    assert claim is not None

    rejected = await coordinator.finish_command(
        claim.command_fence,
        CommandStatus.REJECTED,
        "approval denied",
        '{"error":"approval denied","counted":true}',
    )
    run = await coordinator.get_run("run-1")

    assert rejected.decision is CoordinatorDecision.APPLIED
    assert rejected.value is not None
    assert rejected.value.status is CommandStatus.REJECTED
    assert run is not None
    assert run.observed_state is ObservedState.TERMINAL
    assert run.outcome is RunOutcome.FAILED
    assert run.error == "approval denied"
    assert await coordinator.claim_commands(OwnerLease("executor", clock.value + 10), limit=1) == []


@pytest.mark.asyncio
async def test_command_claim_returns_fence_and_advances_legal_transitions(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    claims = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 30),
        limit=1,
    )

    assert len(claims) == 1
    claim = claims[0]
    assert claim.command.status is CommandStatus.CLAIMED
    assert claim.run.owner_id == "gateway-1"
    assert claim.run.lease_epoch == 1
    assert claim.fence.run_id == "run-1"
    assert claim.fence.owner_id == "gateway-1"
    assert claim.fence.lease_epoch == 1

    starting = await coordinator.mark_starting(
        claim.command, claim.fence, expected_version=claim.run.version
    )
    assert starting.decision is CoordinatorDecision.APPLIED
    assert starting.value is not None
    assert starting.value.observed_state is ObservedState.STARTING

    running = await coordinator.mark_running(
        "run-1", claim.fence, expected_version=starting.value.version
    )
    assert running.decision is CoordinatorDecision.APPLIED
    assert running.value is not None
    assert running.value.observed_state is ObservedState.RUNNING


@pytest.mark.asyncio
async def test_version_and_execution_fences_reject_without_mutation(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    before = await coordinator.get_run("run-1")

    version_conflict = await coordinator.mark_running(
        "run-1", claim.fence, expected_version=running.version - 1
    )
    stale = await coordinator.mark_running(
        "run-1",
        type(claim.fence)(run_id="run-1", owner_id="other", lease_epoch=1),
        expected_version=running.version,
    )

    assert version_conflict.decision is CoordinatorDecision.REJECTED
    assert version_conflict.reason is CoordinatorReason.VERSION_CONFLICT
    assert stale.decision is CoordinatorDecision.REJECTED
    assert stale.reason is CoordinatorReason.STALE_FENCE
    assert await coordinator.get_run("run-1") == before


@pytest.mark.asyncio
async def test_execution_fence_cannot_cross_run_boundaries(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(
        _request(run_id="run-a", command_id="command-a", idempotency_key="key-a")
    )
    await coordinator.submit(
        _request(run_id="run-b", command_id="command-b", idempotency_key="key-b")
    )
    claims = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 30),
        limit=2,
    )
    claim_a, claim_b = claims
    before = await coordinator.get_run("run-b")
    assert before is not None

    starting = await coordinator.mark_starting(
        claim_b.command, claim_a.fence, expected_version=before.version
    )
    running = await coordinator.mark_running(
        "run-b", claim_a.fence, expected_version=before.version
    )
    completed = await coordinator.complete(
        RunCompletion(
            run_id="run-b",
            outcome=RunOutcome.FAILED,
            result_path="",
            error="wrong fence",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"summary":"wrong fence"}',
            terminal_at=clock.value,
        ),
        claim_a.fence,
        expected_version=before.version,
    )
    renewed = await coordinator.renew("run-b", claim_a.fence, clock.value + 60)

    assert starting.reason is CoordinatorReason.STALE_FENCE
    assert running.reason is CoordinatorReason.STALE_FENCE
    assert completed.reason is CoordinatorReason.STALE_FENCE
    assert renewed is False
    assert await coordinator.get_run("run-b") == before


@pytest.mark.asyncio
async def test_expired_command_claim_is_taken_over_and_fences_old_owner(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    first = (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
            limit=1,
        )
    )[0]

    clock.value += 6
    second = (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-2", lease_expires_at=clock.value + 5),
            limit=1,
        )
    )[0]

    stale = await coordinator.mark_starting(
        first.command, first.fence, expected_version=second.run.version
    )
    current = await coordinator.mark_starting(
        second.command, second.fence, expected_version=second.run.version
    )

    assert second.fence.lease_epoch == first.fence.lease_epoch + 1
    assert stale.reason is CoordinatorReason.STALE_FENCE
    assert current.decision is CoordinatorDecision.APPLIED


@pytest.mark.asyncio
async def test_completion_is_first_writer_wins_and_creates_one_event(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    completion = RunCompletion(
        run_id="run-1",
        outcome=RunOutcome.COMPLETED,
        result_path="/tmp/result.txt",
        error="",
        event_type="subagent_completion",
        destination="dashboard:parent",
        payload_json='{"summary":"done"}',
        terminal_at=clock.value,
    )

    completed = await coordinator.complete(
        completion, claim.fence, expected_version=running.version
    )
    replay = await coordinator.complete(
        completion,
        claim.fence,
        expected_version=completed.value.run_version if completed.value else -1,
    )
    conflict = await coordinator.complete(
        replace(completion, outcome=RunOutcome.FAILED, error="late"),
        claim.fence,
        expected_version=completed.value.run_version if completed.value else -1,
    )

    assert completed.decision is CoordinatorDecision.APPLIED
    assert completed.value is not None
    assert completed.value.event_id == "event-1"
    assert completed.value.status is DeliveryState.PENDING
    assert replay.decision is CoordinatorDecision.UNCHANGED
    assert replay.value == completed.value
    assert conflict.decision is CoordinatorDecision.REJECTED
    assert conflict.reason is CoordinatorReason.OUTCOME_CONFLICT


@pytest.mark.asyncio
async def test_outbox_claim_epoch_fences_release_and_delivery(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    completed = await coordinator.complete(
        RunCompletion(
            run_id="run-1",
            outcome=RunOutcome.COMPLETED,
            result_path="/tmp/result.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"summary":"done"}',
            terminal_at=clock.value,
        ),
        claim.fence,
        expected_version=running.version,
    )
    assert completed.value is not None

    first = (
        await coordinator.claim_outbox(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
            limit=1,
        )
    )[0]
    first_fence = DeliveryFence(
        event_id=first.event_id,
        owner_id=first.claim_owner,
        claim_epoch=first.claim_epoch,
    )
    clock.value += 6
    second = (
        await coordinator.claim_outbox(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
            limit=1,
        )
    )[0]
    second_fence = DeliveryFence(
        event_id=second.event_id,
        owner_id=second.claim_owner,
        claim_epoch=second.claim_epoch,
    )

    stale_release = await coordinator.release_outbox(first_fence, available_at=clock.value + 10)
    stale_delivery = await coordinator.mark_delivered(first_fence)
    delivered = await coordinator.mark_delivered(second_fence)
    delivered_again = await coordinator.mark_delivered(second_fence)

    assert second.claim_epoch == first.claim_epoch + 1
    assert stale_release.reason is CoordinatorReason.STALE_FENCE
    assert stale_delivery.reason is CoordinatorReason.STALE_FENCE
    assert delivered.decision is CoordinatorDecision.APPLIED
    assert delivered.value is not None
    assert delivered.value.status is DeliveryState.DELIVERED
    assert delivered_again.decision is CoordinatorDecision.UNCHANGED
    assert (
        await coordinator.claim_outbox(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5),
            limit=1,
        )
        == []
    )


@pytest.mark.asyncio
async def test_claim_limits_and_order_are_deterministic(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    for index in range(3):
        await coordinator.submit(
            _request(
                run_id=f"run-{index}",
                command_id=f"command-{index}",
                idempotency_key=f"key-{index}",
                payload_hash=f"hash-{index}",
            )
        )

    assert (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=0
        )
        == []
    )
    claims = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=2
    )
    assert [item.command.command_id for item in claims] == ["command-0", "command-1"]


@pytest.mark.asyncio
async def test_claim_commands_ignores_non_future_owner_lease(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())

    expired = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value), limit=1
    )
    current = await coordinator.claim_commands(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=1
    )

    assert expired == []
    assert len(current) == 1
    assert current[0].command.attempt == 1
    assert current[0].fence.lease_epoch == 1


@pytest.mark.asyncio
async def test_claim_outbox_ignores_non_future_owner_lease(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    claim, running = await _claimed_running(coordinator, clock)
    await coordinator.complete(
        RunCompletion(
            run_id="run-1",
            outcome=RunOutcome.COMPLETED,
            result_path="/tmp/result.txt",
            error="",
            event_type="subagent_completion",
            destination="dashboard:parent",
            payload_json='{"summary":"done"}',
            terminal_at=clock.value,
        ),
        claim.fence,
        expected_version=running.version,
    )

    expired = await coordinator.claim_outbox(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value), limit=1
    )
    current = await coordinator.claim_outbox(
        OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=1
    )

    assert expired == []
    assert len(current) == 1
    assert current[0].attempts == 1
    assert current[0].claim_epoch == 1


@pytest.mark.asyncio
async def test_renew_requires_current_unexpired_fence(
    coordinator: RunCoordinator,
    clock: FakeClock,
) -> None:
    await coordinator.submit(_request())
    claim = (
        await coordinator.claim_commands(
            OwnerLease(owner_id="gateway-1", lease_expires_at=clock.value + 5), limit=1
        )
    )[0]

    assert await coordinator.renew("run-1", claim.fence, until=clock.value + 20) is True
    clock.value += 21
    assert await coordinator.renew("run-1", claim.fence, until=clock.value + 20) is False
