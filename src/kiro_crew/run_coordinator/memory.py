"""Deterministic in-memory implementation of the run coordinator contract."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import replace

from .models import (
    LEGACY_SHADOW_SOURCE_VERSION,
    CommandClaim,
    CommandFence,
    CommandOperation,
    CommandReceipt,
    CommandStatus,
    CoordinatorDecision,
    CoordinatorReason,
    CoordinatorResult,
    DeliveryFence,
    DeliveryState,
    DesiredState,
    LegacyImportReceipt,
    LegacyRunImport,
    ObservedState,
    OutboxEvent,
    OwnerLease,
    RecoveryClaim,
    RunCommand,
    RunCompletion,
    RunFence,
    RunOutcome,
    RunRecord,
    SubmitControl,
    SubmitReceipt,
    SubmitRun,
)

_EXECUTION_COMMANDS = frozenset({CommandOperation.SPAWN, CommandOperation.CONTINUE})
_CONTROL_COMMANDS = frozenset(
    {CommandOperation.STEER, CommandOperation.CANCEL, CommandOperation.RELEASE}
)
_LEGACY_SHADOW_RECOVERY_GRACE_SECONDS = 60.0
_STARTABLE_STATES = frozenset({ObservedState.ACCEPTED, ObservedState.QUEUED})
_COMPLETABLE_STATES = frozenset(
    {
        ObservedState.ACCEPTED,
        ObservedState.QUEUED,
        ObservedState.STARTING,
        ObservedState.RUNNING,
    }
)


class MemoryRunCoordinator:
    """Event-loop-affine coordinator used by tests and pre-authority wiring."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._lock = asyncio.Lock()
        self._runs: dict[str, RunRecord] = {}
        self._commands: dict[str, RunCommand] = {}
        self._command_by_key: dict[str, str] = {}
        self._outbox: dict[str, OutboxEvent] = {}
        self._outbox_by_run_type: dict[tuple[str, str], str] = {}

    @staticmethod
    def _result(
        decision: CoordinatorDecision,
        reason: CoordinatorReason,
        value: object = None,
    ) -> CoordinatorResult:
        return CoordinatorResult(decision=decision, reason=reason, value=value)

    async def submit(self, request: SubmitRun) -> CoordinatorResult[SubmitReceipt]:
        async with self._lock:
            if request.operation not in _EXECUTION_COMMANDS:
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.UNSUPPORTED_OPERATION,
                )
            existing_id = self._command_by_key.get(request.idempotency_key)
            if existing_id is not None:
                command = self._commands[existing_id]
                if (
                    command.operation is not request.operation
                    or command.payload_hash != request.payload_hash
                ):
                    return self._result(
                        CoordinatorDecision.REJECTED,
                        CoordinatorReason.IDEMPOTENCY_CONFLICT,
                    )
                run = self._runs[command.run_id]
                return self._result(
                    CoordinatorDecision.UNCHANGED,
                    CoordinatorReason.IDEMPOTENT_REPLAY,
                    SubmitReceipt(run=run, command=command, created=False),
                )
            if request.run_id in self._runs or request.command_id in self._commands:
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.IDENTITY_CONFLICT,
                )

            now = self._clock()
            accepted = request.accepted
            run = RunRecord(
                run_id=request.run_id,
                parent_session=request.parent_session,
                agent=request.agent,
                task=request.task,
                conversation_key=request.conversation_key,
                desired_state=DesiredState.RUN,
                observed_state=(ObservedState.ACCEPTED if accepted else ObservedState.TERMINAL),
                outcome=None if accepted else RunOutcome.FAILED,
                result_path="",
                error=request.rejection_reason,
                attempt=1,
                version=1,
                owner_id="",
                lease_expires_at=0.0,
                lease_epoch=0,
                created_at=now,
                updated_at=now,
                terminal_at=None if accepted else now,
                source_version=request.source_version,
            )
            command = RunCommand(
                command_id=request.command_id,
                idempotency_key=request.idempotency_key,
                run_id=request.run_id,
                operation=request.operation,
                payload_hash=request.payload_hash,
                status=CommandStatus.PENDING if accepted else CommandStatus.REJECTED,
                attempt=0,
                owner_id="",
                lease_epoch=0,
                created_at=now,
                updated_at=now,
                rejection_reason=request.rejection_reason,
                payload_json=request.payload_json,
            )
            self._runs[run.run_id] = run
            self._commands[command.command_id] = command
            self._command_by_key[command.idempotency_key] = command.command_id
            receipt = SubmitReceipt(run=run, command=command, created=True)
            if not accepted:
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.ADMISSION_REJECTED,
                    receipt,
                )
            return self._result(CoordinatorDecision.APPLIED, CoordinatorReason.CREATED, receipt)

    async def submit_control(self, request: SubmitControl) -> CoordinatorResult[CommandReceipt]:
        async with self._lock:
            if request.operation not in _CONTROL_COMMANDS:
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.UNSUPPORTED_OPERATION,
                )
            existing_id = self._command_by_key.get(request.idempotency_key)
            if existing_id is not None:
                command = self._commands[existing_id]
                if (
                    command.run_id != request.run_id
                    or command.operation is not request.operation
                    or command.payload_hash != request.payload_hash
                ):
                    return self._result(
                        CoordinatorDecision.REJECTED,
                        CoordinatorReason.IDEMPOTENCY_CONFLICT,
                    )
                return self._result(
                    CoordinatorDecision.UNCHANGED,
                    CoordinatorReason.IDEMPOTENT_REPLAY,
                    CommandReceipt(
                        run=self._runs.get(command.run_id),
                        command=command,
                        created=False,
                    ),
                )
            if request.command_id in self._commands:
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.IDENTITY_CONFLICT,
                )

            now = self._clock()
            command = RunCommand(
                command_id=request.command_id,
                idempotency_key=request.idempotency_key,
                run_id=request.run_id,
                operation=request.operation,
                payload_hash=request.payload_hash,
                status=(CommandStatus.PENDING if request.accepted else CommandStatus.REJECTED),
                attempt=0,
                owner_id="",
                lease_epoch=0,
                created_at=now,
                updated_at=now,
                rejection_reason=request.rejection_reason,
                payload_json=request.payload_json,
            )
            self._commands[command.command_id] = command
            self._command_by_key[command.idempotency_key] = command.command_id
            receipt = CommandReceipt(
                run=self._runs.get(command.run_id), command=command, created=True
            )
            if not request.accepted:
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.ADMISSION_REJECTED,
                    receipt,
                )
            return self._result(CoordinatorDecision.APPLIED, CoordinatorReason.CREATED, receipt)

    async def import_legacy(
        self, request: LegacyRunImport
    ) -> CoordinatorResult[LegacyImportReceipt]:
        """Create a legacy-only run without manufacturing an execution command."""

        async with self._lock:
            terminal = request.observed_state is ObservedState.TERMINAL
            if terminal != (request.outcome is not None):
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.INVALID_TRANSITION,
                )
            if bool(request.event_type) != bool(request.delivery_state):
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.INVALID_TRANSITION,
                )
            existing = self._runs.get(request.run_id)
            if existing is not None:
                event_id = self._outbox_by_run_type.get((request.run_id, request.event_type))
                existing_event = self._outbox.get(event_id) if event_id else None
                # Legacy files are evidence, not lifecycle authority. A crash
                # can leave a durable coordinator row before result.txt is
                # written, so retain that path for the fenced recovery which
                # follows. Never copy terminal state, outcome, error, owner, or
                # version from an agent-writable tombstone into an existing row.
                if (
                    existing.observed_state is not ObservedState.TERMINAL
                    and not existing.result_path
                    and request.result_path
                ):
                    existing = replace(existing, result_path=request.result_path)
                    self._runs[request.run_id] = existing
                    return self._result(
                        CoordinatorDecision.APPLIED,
                        CoordinatorReason.TRANSITIONED,
                        LegacyImportReceipt(existing, existing_event, created=False),
                    )
                return self._result(
                    CoordinatorDecision.UNCHANGED,
                    CoordinatorReason.IDEMPOTENT_REPLAY,
                    LegacyImportReceipt(existing, existing_event, created=False),
                )
            run = RunRecord(
                run_id=request.run_id,
                parent_session=request.parent_session,
                agent=request.agent,
                task=request.task,
                conversation_key=request.conversation_key,
                desired_state=DesiredState.RUN,
                observed_state=request.observed_state,
                outcome=request.outcome,
                result_path=request.result_path,
                error=request.error,
                attempt=1,
                version=1,
                owner_id="",
                lease_expires_at=0.0,
                lease_epoch=0,
                created_at=request.created_at,
                updated_at=request.updated_at,
                terminal_at=request.terminal_at,
                source_version=request.source_version,
            )
            self._runs[run.run_id] = run
            event: OutboxEvent | None = None
            if request.event_type and request.delivery_state is not None:
                delivered = request.delivery_state is DeliveryState.DELIVERED
                event = OutboxEvent(
                    event_id=self._id_factory(),
                    run_id=run.run_id,
                    run_version=run.version,
                    destination=request.destination,
                    event_type=request.event_type,
                    payload_json=request.payload_json,
                    status=request.delivery_state,
                    attempts=1 if delivered else 0,
                    available_at=request.updated_at,
                    claim_owner="",
                    claim_expires_at=0.0,
                    claim_epoch=0,
                    created_at=request.updated_at,
                    delivered_at=request.terminal_at if delivered else None,
                )
                self._outbox[event.event_id] = event
                self._outbox_by_run_type[(run.run_id, event.event_type)] = event.event_id
            return self._result(
                CoordinatorDecision.APPLIED,
                CoordinatorReason.CREATED,
                LegacyImportReceipt(run, event, created=True),
            )

    async def get_command_by_key(self, idempotency_key: str) -> CommandReceipt | None:
        async with self._lock:
            command_id = self._command_by_key.get(idempotency_key)
            if command_id is None:
                return None
            command = self._commands[command_id]
            return CommandReceipt(
                run=self._runs.get(command.run_id), command=command, created=False
            )

    async def claim_commands(self, owner: OwnerLease, limit: int) -> list[CommandClaim]:
        return await self._claim_commands(
            owner,
            limit,
            controls=False,
            acquire_run_lease=True,
        )

    async def claim_controls(
        self, owner: OwnerLease, limit: int, command_id: str = ""
    ) -> list[CommandClaim]:
        return await self._claim_commands(
            owner,
            limit,
            controls=True,
            command_id=command_id,
            acquire_run_lease=False,
        )

    async def claim_command(self, command_id: str, owner: OwnerLease) -> CommandClaim | None:
        command = self._commands.get(command_id)
        acquire_run_lease = bool(command is not None and command.operation in _EXECUTION_COMMANDS)
        claims = await self._claim_commands(
            owner,
            1,
            controls=None,
            command_id=command_id,
            acquire_run_lease=acquire_run_lease,
        )
        return claims[0] if claims else None

    async def claim_recovery(
        self,
        owner: OwnerLease,
        limit: int,
        exclude_run_ids: frozenset[str] = frozenset(),
    ) -> list[RecoveryClaim]:
        """Fence expired nonterminal runs without claiming or replaying commands."""

        if limit <= 0:
            return []
        async with self._lock:
            now = self._clock()
            if owner.lease_expires_at <= now:
                return []
            pending_execution_run_ids = {
                command.run_id
                for command in self._commands.values()
                if command.operation in _EXECUTION_COMMANDS
                and command.status is CommandStatus.PENDING
                and (
                    self._runs[command.run_id].source_version != LEGACY_SHADOW_SOURCE_VERSION
                    or self._runs[command.run_id].created_at
                    > now - _LEGACY_SHADOW_RECOVERY_GRACE_SECONDS
                )
            }
            claims: list[RecoveryClaim] = []
            for current in sorted(
                self._runs.values(), key=lambda item: (item.created_at, item.run_id)
            ):
                if len(claims) >= limit:
                    break
                if (
                    current.run_id in exclude_run_ids
                    or current.run_id in pending_execution_run_ids
                    or current.observed_state is ObservedState.TERMINAL
                    or current.lease_expires_at > now
                ):
                    continue
                run = replace(
                    current,
                    owner_id=owner.owner_id,
                    lease_expires_at=owner.lease_expires_at,
                    lease_epoch=current.lease_epoch + 1,
                    updated_at=now,
                )
                self._runs[run.run_id] = run
                claims.append(
                    RecoveryClaim(
                        run=run,
                        fence=RunFence(run.run_id, owner.owner_id, run.lease_epoch),
                    )
                )
            return claims

    async def _claim_commands(
        self,
        owner: OwnerLease,
        limit: int,
        *,
        controls: bool | None,
        command_id: str = "",
        acquire_run_lease: bool,
    ) -> list[CommandClaim]:
        if limit <= 0:
            return []
        async with self._lock:
            now = self._clock()
            if owner.lease_expires_at <= now:
                return []
            claims: list[CommandClaim] = []
            for current in sorted(
                self._commands.values(),
                key=lambda item: (item.created_at, item.command_id),
            ):
                if len(claims) >= limit:
                    break
                is_control = current.operation in _CONTROL_COMMANDS
                if (controls is not None and is_control is not controls) or (
                    command_id and current.command_id != command_id
                ):
                    continue
                run = self._runs.get(current.run_id)
                if not (
                    current.status is CommandStatus.PENDING
                    or (current.status is CommandStatus.CLAIMED and current.claim_expires_at <= now)
                ):
                    continue
                claim_epoch = current.claim_epoch + 1
                fence: RunFence | None = None
                legacy_lease_epoch = 0
                if acquire_run_lease:
                    if run is None:
                        continue
                    legacy_lease_epoch = run.lease_epoch + 1
                    run = replace(
                        run,
                        owner_id=owner.owner_id,
                        lease_expires_at=owner.lease_expires_at,
                        lease_epoch=legacy_lease_epoch,
                        updated_at=now,
                    )
                    self._runs[run.run_id] = run
                    fence = RunFence(run.run_id, owner.owner_id, legacy_lease_epoch)
                command = replace(
                    current,
                    status=CommandStatus.CLAIMED,
                    attempt=current.attempt + 1,
                    owner_id=owner.owner_id,
                    lease_epoch=legacy_lease_epoch,
                    claim_expires_at=owner.lease_expires_at,
                    claim_epoch=claim_epoch,
                    updated_at=now,
                )
                self._commands[command.command_id] = command
                claims.append(
                    CommandClaim(
                        command=command,
                        run=run,
                        fence=fence,
                        command_fence=CommandFence(
                            command.command_id,
                            owner.owner_id,
                            claim_epoch,
                        ),
                    )
                )
            return claims

    async def finish_control(
        self,
        fence: CommandFence,
        status: CommandStatus,
        rejection_reason: str = "",
        result_json: str = "",
    ) -> CoordinatorResult[RunCommand]:
        command = self._commands.get(fence.command_id)
        if command is None or command.operation not in _CONTROL_COMMANDS:
            return self._result(
                CoordinatorDecision.REJECTED,
                CoordinatorReason.INVALID_TRANSITION,
            )
        return await self.finish_command(
            fence,
            status,
            rejection_reason,
            result_json,
        )

    async def finish_command(
        self,
        fence: CommandFence,
        status: CommandStatus,
        rejection_reason: str = "",
        result_json: str = "",
    ) -> CoordinatorResult[RunCommand]:
        async with self._lock:
            command = self._commands.get(fence.command_id)
            if command is None:
                return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.NOT_FOUND)
            matching = (
                command.owner_id == fence.owner_id and command.claim_epoch == fence.claim_epoch
            )
            if command.status in (CommandStatus.APPLIED, CommandStatus.REJECTED):
                if matching:
                    result_fill = (
                        command.status is CommandStatus.APPLIED
                        and status is CommandStatus.APPLIED
                        and not command.rejection_reason
                        and not command.result_json
                        and not rejection_reason
                        and bool(result_json)
                    )
                    if result_fill and command.operation in _EXECUTION_COMMANDS:
                        run = self._runs.get(command.run_id)
                        if (
                            run is None
                            or run.owner_id != command.owner_id
                            or run.lease_epoch != command.lease_epoch
                        ):
                            return self._result(
                                CoordinatorDecision.REJECTED,
                                CoordinatorReason.STALE_FENCE,
                            )
                    if result_fill:
                        command = replace(
                            command,
                            result_json=result_json,
                            updated_at=self._clock(),
                        )
                        self._commands[command.command_id] = command
                        return self._result(
                            CoordinatorDecision.APPLIED,
                            CoordinatorReason.TRANSITIONED,
                            command,
                        )
                    if (
                        command.status is not status
                        or command.rejection_reason != rejection_reason
                        or command.result_json != result_json
                    ):
                        return self._result(
                            CoordinatorDecision.REJECTED,
                            CoordinatorReason.OUTCOME_CONFLICT,
                        )
                    return self._result(
                        CoordinatorDecision.UNCHANGED,
                        CoordinatorReason.TRANSITIONED,
                        command,
                    )
                return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
            if command.status is not CommandStatus.CLAIMED or not matching:
                return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
            if status not in (CommandStatus.APPLIED, CommandStatus.REJECTED):
                return self._result(
                    CoordinatorDecision.REJECTED,
                    CoordinatorReason.INVALID_TRANSITION,
                )
            command = replace(
                command,
                status=status,
                rejection_reason=rejection_reason,
                result_json=result_json,
                updated_at=self._clock(),
            )
            self._commands[command.command_id] = command
            return self._result(
                CoordinatorDecision.APPLIED,
                CoordinatorReason.TRANSITIONED,
                command,
            )

    def _validate_transition(
        self,
        run_id: str,
        fence: RunFence,
        expected_version: int,
        *,
        allow_expired: bool = False,
    ) -> CoordinatorResult[RunRecord] | RunRecord:
        run = self._runs.get(run_id)
        if run is None:
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.NOT_FOUND)
        if (
            fence.run_id != run_id
            or run.owner_id != fence.owner_id
            or run.lease_epoch != fence.lease_epoch
            or (not allow_expired and run.lease_expires_at <= self._clock())
        ):
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
        if run.version != expected_version:
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.VERSION_CONFLICT)
        return run

    async def mark_starting(
        self, command: RunCommand, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]:
        async with self._lock:
            validated = self._validate_transition(command.run_id, fence, expected_version)
            if isinstance(validated, CoordinatorResult):
                current = self._runs.get(command.run_id)
                if not (
                    validated.reason is CoordinatorReason.VERSION_CONFLICT
                    and current is not None
                    and current.version == expected_version + 1
                    and current.observed_state is ObservedState.STARTING
                ):
                    return validated
                validated = current
            stored = self._commands.get(command.command_id)
            if (
                stored is None
                or stored.run_id != command.run_id
                or stored.operation not in _EXECUTION_COMMANDS
                or stored.owner_id != fence.owner_id
                or stored.claim_epoch != command.claim_epoch
                or stored.status not in (CommandStatus.CLAIMED, CommandStatus.APPLIED)
            ):
                return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
            if validated.observed_state is ObservedState.STARTING:
                return self._result(
                    CoordinatorDecision.UNCHANGED, CoordinatorReason.TRANSITIONED, validated
                )
            if validated.observed_state not in _STARTABLE_STATES:
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.INVALID_TRANSITION
                )
            updated = replace(
                validated,
                observed_state=ObservedState.STARTING,
                version=validated.version + 1,
                updated_at=self._clock(),
            )
            self._runs[updated.run_id] = updated
            return self._result(
                CoordinatorDecision.APPLIED, CoordinatorReason.TRANSITIONED, updated
            )

    async def mark_running(
        self, run_id: str, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]:
        async with self._lock:
            validated = self._validate_transition(run_id, fence, expected_version)
            if isinstance(validated, CoordinatorResult):
                current = self._runs.get(run_id)
                if not (
                    validated.reason is CoordinatorReason.VERSION_CONFLICT
                    and current is not None
                    and current.version == expected_version + 1
                    and current.observed_state is ObservedState.RUNNING
                ):
                    return validated
                validated = current
            if validated.observed_state is ObservedState.RUNNING:
                return self._result(
                    CoordinatorDecision.UNCHANGED, CoordinatorReason.TRANSITIONED, validated
                )
            if validated.observed_state is not ObservedState.STARTING:
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.INVALID_TRANSITION
                )
            updated = replace(
                validated,
                observed_state=ObservedState.RUNNING,
                version=validated.version + 1,
                updated_at=self._clock(),
            )
            self._runs[updated.run_id] = updated
            return self._result(
                CoordinatorDecision.APPLIED, CoordinatorReason.TRANSITIONED, updated
            )

    async def record_process(
        self,
        run_id: str,
        fence: RunFence,
        expected_version: int,
        process_id: int,
        process_start_id: str,
        process_owned: bool,
    ) -> CoordinatorResult[RunRecord]:
        async with self._lock:
            validated = self._validate_transition(run_id, fence, expected_version)
            if isinstance(validated, CoordinatorResult):
                current = self._runs.get(run_id)
                if not (
                    validated.reason is CoordinatorReason.VERSION_CONFLICT
                    and current is not None
                    and current.version == expected_version + 1
                    and current.observed_state is ObservedState.RUNNING
                    and current.process_id is not None
                ):
                    return validated
                # A process record is the only lifecycle write that can advance
                # a RUNNING row by one version. The SQLite worker may commit that
                # write after its cancelled await returns no value; the same
                # execution fence must be able to observe it or replace it for
                # the manager's one-shot fresh-session recovery.
                validated = current
            if (
                type(process_id) is not int
                or process_id <= 1
                or not isinstance(process_start_id, str)
                or (process_owned and not process_start_id)
            ):
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.INVALID_TRANSITION
                )
            if validated.observed_state is not ObservedState.RUNNING:
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.INVALID_TRANSITION
                )
            if (
                validated.process_id == process_id
                and validated.process_start_id == process_start_id
                and validated.process_owned is process_owned
            ):
                return self._result(
                    CoordinatorDecision.UNCHANGED, CoordinatorReason.TRANSITIONED, validated
                )
            updated = replace(
                validated,
                process_id=process_id,
                process_start_id=process_start_id,
                process_owned=process_owned,
                version=validated.version + 1,
                updated_at=self._clock(),
            )
            self._runs[updated.run_id] = updated
            return self._result(
                CoordinatorDecision.APPLIED, CoordinatorReason.TRANSITIONED, updated
            )

    async def complete(
        self, completion: RunCompletion, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[OutboxEvent]:
        async with self._lock:
            key = (completion.run_id, completion.event_type)
            existing_id = self._outbox_by_run_type.get(key)
            if existing_id is not None:
                event = self._outbox[existing_id]
                run = self._runs[completion.run_id]
                if (
                    fence.run_id != run.run_id
                    or fence.owner_id != run.owner_id
                    or fence.lease_epoch != run.lease_epoch
                ):
                    return self._result(
                        CoordinatorDecision.REJECTED,
                        CoordinatorReason.STALE_FENCE,
                    )
                if (
                    run.outcome is completion.outcome
                    and run.result_path == completion.result_path
                    and run.error == completion.error
                    and event.destination == completion.destination
                    and event.payload_json == completion.payload_json
                ):
                    return self._result(
                        CoordinatorDecision.UNCHANGED,
                        CoordinatorReason.COMPLETION_REPLAY,
                        event,
                    )
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.OUTCOME_CONFLICT
                )
            # Expiry makes a run eligible for takeover; the monotonic epoch is
            # the fence. Let the current epoch commit its terminal result when
            # no recovery owner won that race, including after host suspend.
            validated = self._validate_transition(
                completion.run_id,
                fence,
                expected_version,
                allow_expired=True,
            )
            if isinstance(validated, CoordinatorResult):
                return self._result(validated.decision, validated.reason)
            if validated.observed_state not in _COMPLETABLE_STATES:
                return self._result(
                    CoordinatorDecision.REJECTED, CoordinatorReason.INVALID_TRANSITION
                )
            now = self._clock()
            run = replace(
                validated,
                observed_state=ObservedState.TERMINAL,
                outcome=completion.outcome,
                result_path=completion.result_path,
                error=completion.error,
                version=validated.version + 1,
                updated_at=now,
                terminal_at=completion.terminal_at,
            )
            self._runs[run.run_id] = run
            for command_id, command in tuple(self._commands.items()):
                if (
                    command.run_id == run.run_id
                    and command.operation in _EXECUTION_COMMANDS
                    and command.status is CommandStatus.CLAIMED
                ):
                    self._commands[command_id] = replace(
                        command, status=CommandStatus.APPLIED, updated_at=now
                    )
            event = OutboxEvent(
                event_id=self._id_factory(),
                run_id=run.run_id,
                run_version=run.version,
                destination=completion.destination,
                event_type=completion.event_type,
                payload_json=completion.payload_json,
                status=completion.delivery_state,
                attempts=0,
                available_at=now,
                claim_owner="",
                claim_expires_at=0.0,
                claim_epoch=0,
                created_at=now,
                delivered_at=(
                    now if completion.delivery_state is DeliveryState.DELIVERED else None
                ),
            )
            self._outbox[event.event_id] = event
            self._outbox_by_run_type[key] = event.event_id
            return self._result(CoordinatorDecision.APPLIED, CoordinatorReason.COMPLETED, event)

    async def renew(self, run_id: str, fence: RunFence, until: float) -> bool:
        async with self._lock:
            run = self._runs.get(run_id)
            now = self._clock()
            if (
                run is None
                or fence.run_id != run_id
                or run.owner_id != fence.owner_id
                or run.lease_epoch != fence.lease_epoch
                or run.lease_expires_at <= now
                or until <= now
            ):
                return False
            self._runs[run_id] = replace(run, lease_expires_at=until, updated_at=now)
            for command_id, command in tuple(self._commands.items()):
                if (
                    command.run_id == run_id
                    and command.operation in _EXECUTION_COMMANDS
                    and command.status is CommandStatus.CLAIMED
                    and command.owner_id == fence.owner_id
                    and command.lease_epoch == fence.lease_epoch
                ):
                    self._commands[command_id] = replace(
                        command,
                        claim_expires_at=until,
                        updated_at=now,
                    )
            return True

    async def claim_outbox(
        self,
        owner: OwnerLease,
        limit: int,
        event_id: str = "",
        acknowledgement: bool = False,
    ) -> list[OutboxEvent]:
        if limit <= 0:
            return []
        async with self._lock:
            now = self._clock()
            if owner.lease_expires_at <= now:
                return []
            claimed: list[OutboxEvent] = []
            for current in sorted(
                self._outbox.values(), key=lambda item: (item.created_at, item.event_id)
            ):
                if len(claimed) >= limit:
                    break
                if event_id and current.event_id != event_id:
                    continue
                pending = current.status is DeliveryState.PENDING and (
                    (acknowledgement and bool(event_id)) or current.available_at <= now
                )
                expired = (
                    current.status is DeliveryState.CLAIMED and current.claim_expires_at <= now
                )
                if not (pending or expired):
                    continue
                event = replace(
                    current,
                    status=DeliveryState.CLAIMED,
                    attempts=current.attempts + 1,
                    claim_owner=owner.owner_id,
                    claim_expires_at=owner.lease_expires_at,
                    claim_epoch=current.claim_epoch + 1,
                )
                self._outbox[event.event_id] = event
                claimed.append(event)
            return claimed

    def _validate_delivery(
        self, fence: DeliveryFence
    ) -> CoordinatorResult[OutboxEvent] | OutboxEvent:
        event = self._outbox.get(fence.event_id)
        if event is None:
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.NOT_FOUND)
        matching = event.claim_owner == fence.owner_id and event.claim_epoch == fence.claim_epoch
        if event.status is DeliveryState.DELIVERED:
            if matching:
                return self._result(
                    CoordinatorDecision.UNCHANGED,
                    CoordinatorReason.ALREADY_DELIVERED,
                    event,
                )
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
        if (
            event.status is not DeliveryState.CLAIMED
            or not matching
            or event.claim_expires_at <= self._clock()
        ):
            return self._result(CoordinatorDecision.REJECTED, CoordinatorReason.STALE_FENCE)
        return event

    async def release_outbox(
        self, fence: DeliveryFence, available_at: float
    ) -> CoordinatorResult[OutboxEvent]:
        async with self._lock:
            validated = self._validate_delivery(fence)
            if isinstance(validated, CoordinatorResult):
                return validated
            event = replace(
                validated,
                status=DeliveryState.PENDING,
                available_at=available_at,
                claim_owner="",
                claim_expires_at=0.0,
            )
            self._outbox[event.event_id] = event
            return self._result(
                CoordinatorDecision.APPLIED, CoordinatorReason.DELIVERY_RELEASED, event
            )

    async def mark_delivered(self, fence: DeliveryFence) -> CoordinatorResult[OutboxEvent]:
        async with self._lock:
            validated = self._validate_delivery(fence)
            if isinstance(validated, CoordinatorResult):
                return validated
            event = replace(
                validated,
                status=DeliveryState.DELIVERED,
                delivered_at=self._clock(),
            )
            self._outbox[event.event_id] = event
            return self._result(CoordinatorDecision.APPLIED, CoordinatorReason.DELIVERED, event)

    async def get_run(self, run_id: str) -> RunRecord | None:
        async with self._lock:
            return self._runs.get(run_id)
