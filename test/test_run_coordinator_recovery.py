"""Coordinator-first restart recovery and legacy import contracts."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import platform_compat
from kiro_crew.run_coordinator import (
    CommandOperation,
    CoordinatorDecision,
    MemoryRunCoordinator,
    ObservedState,
    OwnerLease,
    RunOutcome,
    SubmitRun,
)
from kiro_crew.run_coordinator.delivery import OutboxDeliveryAdapter
from kiro_crew.run_coordinator.legacy import LegacyRunImporter
from kiro_crew.run_coordinator.recovery import RunRecovery


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _legacy_state(root: Path, run_id: str, **fields: object) -> Path:
    folder = root / run_id
    _write_json(
        folder / "state.json",
        {
            "id": run_id,
            "task": "recover this work",
            "agent": "kirocrew",
            "parent_session": "dashboard:default",
            "started": 10.0,
            "updated_at": 20.0,
            "status": "running",
            "pid": None,
            **fields,
        },
    )
    return folder


async def _record_protected_process(
    coordinator: MemoryRunCoordinator,
    clock: list[float],
    run_id: str,
    process_id: int,
    process_start_id: str,
    *,
    process_owned: bool = True,
) -> None:
    submitted = await coordinator.submit(
        SubmitRun(
            run_id=run_id,
            command_id=f"command-{run_id}",
            idempotency_key=f"key-{run_id}",
            payload_hash=f"hash-{run_id}",
            parent_session="dashboard:default",
            agent="kirocrew",
            task="recover this work",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    claim = await coordinator.claim_command(
        f"command-{run_id}",
        OwnerLease("executor", clock[0] + 10.0),
    )
    assert claim is not None
    starting = await coordinator.mark_starting(
        claim.command,
        claim.fence,
        claim.run.version,
    )
    assert starting.value is not None
    running = await coordinator.mark_running(run_id, claim.fence, starting.value.version)
    assert running.value is not None
    recorded = await coordinator.record_process(
        run_id,
        claim.fence,
        running.value.version,
        process_id,
        process_start_id,
        process_owned,
    )
    assert recorded.value is not None
    clock[0] += 11.0


@pytest.mark.asyncio
async def test_legacy_import_is_idempotent_and_leaves_files_byte_identical(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    _legacy_state(tmp_path, "running1", keep=True)
    delivered = _legacy_state(tmp_path, "done1")
    (delivered / "result.txt").write_text("finished", encoding="utf-8")
    _write_json(
        delivered / "tombstone.json",
        {
            "id": "done1",
            "cause": "delivered",
            "recovery_action": "delivered",
            "died": 30.0,
            "result_available": True,
            "result_path": str(delivered / "result.txt"),
        },
    )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    importer = LegacyRunImporter(coordinator, root=tmp_path, clock=lambda: clock[0])
    first = await importer.import_all()
    second = await importer.import_all()

    assert first.imported == 2
    assert second.imported == 0
    assert second.existing == 2
    assert await coordinator.get_run("running1") is not None
    terminal = await coordinator.get_run("done1")
    assert terminal is not None
    assert terminal.observed_state is ObservedState.TERMINAL
    assert terminal.outcome is RunOutcome.COMPLETED
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.asyncio
async def test_legacy_process_metadata_never_authorizes_termination(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    _legacy_state(
        tmp_path,
        "failed-live",
        pid=4321,
        pid_start_id="same",
        process_owned=True,
    )
    importer = LegacyRunImporter(coordinator, root=tmp_path, clock=lambda: clock[0])
    imported = await importer.import_all()
    run = await coordinator.get_run("failed-live")

    assert imported.imported == 1
    assert run is not None
    assert run.observed_state is ObservedState.RUNNING
    assert run.outcome is None
    assert run.process_id == 0
    assert run.process_owned is False

    terminated: list[int] = []
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    recovered = await coordinator.get_run("failed-live")

    assert report.terminated == 0
    assert report.interrupted == 1
    assert terminated == []
    assert recovered is not None
    assert recovered.outcome is RunOutcome.INTERRUPTED


@pytest.mark.asyncio
async def test_legacy_import_skips_corrupt_and_traversal_like_folders(tmp_path, caplog):
    bad = tmp_path / "corrupt"
    bad.mkdir()
    (bad / "state.json").write_text("{broken", encoding="utf-8")
    mismatched = _legacy_state(tmp_path, "folder-id")
    state = json.loads((mismatched / "state.json").read_text(encoding="utf-8"))
    state["id"] = "../escape"
    _write_json(mismatched / "state.json", state)
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.corrupt == 2
    assert report.imported == 0
    assert "legacy subagent import skipped" in caplog.text


@pytest.mark.asyncio
async def test_legacy_import_skips_deep_json_without_blocking_siblings(tmp_path):
    deep = tmp_path / "deep-json"
    deep.mkdir()
    depth = 2000
    (deep / "state.json").write_text(
        "[" * depth + "0" + "]" * depth,
        encoding="utf-8",
    )
    _legacy_state(tmp_path, "good-json")
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.corrupt == 1
    assert report.imported == 1
    assert await coordinator.get_run("good-json") is not None


@pytest.mark.asyncio
async def test_legacy_import_bounds_and_pins_json_reads(tmp_path):
    import_root = tmp_path / "legacy"
    import_root.mkdir()
    oversized = import_root / "oversized"
    oversized.mkdir()
    (oversized / "state.json").write_bytes(b" " * (1024 * 1024 + 1))

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "state.json"
    _write_json(outside, {"id": "linked"})
    linked = import_root / "linked"
    linked.mkdir()
    (linked / "state.json").symlink_to(outside)

    _legacy_state(import_root, "good-bounded")
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=import_root).import_all()

    assert report.corrupt == 2
    assert report.imported == 1
    assert await coordinator.get_run("good-bounded") is not None


@pytest.mark.asyncio
async def test_legacy_import_skips_non_finite_timestamp_without_blocking_siblings(tmp_path):
    _legacy_state(tmp_path, "bad-time", started=float("nan"))
    _legacy_state(tmp_path, "good-time")
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.corrupt == 1
    assert report.imported == 1
    assert await coordinator.get_run("bad-time") is None
    assert await coordinator.get_run("good-time") is not None


@pytest.mark.asyncio
async def test_legacy_import_skips_oversized_timestamp_without_blocking_siblings(tmp_path):
    bad = _legacy_state(tmp_path, "bad-time", started=10**400)
    _legacy_state(tmp_path, "good-time")
    before = (bad / "state.json").read_bytes()
    coordinator = MemoryRunCoordinator()

    report = await LegacyRunImporter(coordinator, root=tmp_path).import_all()

    assert report.corrupt == 1
    assert report.imported == 1
    assert await coordinator.get_run("bad-time") is None
    assert await coordinator.get_run("good-time") is not None
    assert (bad / "state.json").read_bytes() == before


@pytest.mark.asyncio
async def test_recovery_takeover_fences_old_owner_without_replaying_command(tmp_path):
    clock = [10.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    submitted = await coordinator.submit(
        SubmitRun(
            run_id="run1",
            command_id="command1",
            idempotency_key="key1",
            payload_hash="hash1",
            parent_session="dashboard:default",
            agent="kirocrew",
            task="work",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    assert submitted.value is not None
    old_claim = await coordinator.claim_command("command1", OwnerLease("old", 20.0))
    assert old_claim is not None
    clock[0] = 21.0
    delivered = AsyncMock(return_value=True)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            delivered,
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
    )

    report = await recovery.reconcile()

    assert report.interrupted == 1
    run = await coordinator.get_run("run1")
    assert run is not None
    assert run.outcome is RunOutcome.INTERRUPTED
    assert run.lease_epoch == old_claim.fence.lease_epoch + 1
    stale = await coordinator.complete(
        recovery.completion_for(run, outcome=RunOutcome.COMPLETED),
        old_claim.fence,
        old_claim.run.version,
    )
    assert stale.decision is CoordinatorDecision.REJECTED
    stale_exact = await coordinator.complete(
        recovery.completion_for(run),
        old_claim.fence,
        old_claim.run.version,
    )
    assert stale_exact.decision is CoordinatorDecision.REJECTED
    assert delivered.await_count == 1
    receipt = await coordinator.get_command_by_key("key1")
    assert receipt is not None
    assert receipt.command.attempt == 1


@pytest.mark.asyncio
async def test_recovery_terminates_only_verified_live_child(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    terminated: list[int] = []
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()

    assert report.terminated == 1
    assert terminated == [4321]


@pytest.mark.asyncio
async def test_recovery_refuses_termination_when_sel_audit_fails(tmp_path, monkeypatch):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    terminated: list[int] = []
    audit = MagicMock()
    audit.log_tool_invocation.side_effect = OSError("audit unavailable")
    monkeypatch.setattr("kiro_crew.run_coordinator.recovery.sel", lambda: audit)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.terminated == 0
    assert terminated == []
    assert run is not None
    assert run.outcome is None


@pytest.mark.asyncio
async def test_recovery_default_termination_pins_the_recorded_process_identity(
    tmp_path,
    monkeypatch,
):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    calls: list[tuple[int, str, int]] = []

    def pinned_kill(pid: int, start_id: str, sig: int) -> bool:
        calls.append((pid, start_id, sig))
        return True

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "kill_process_tree_pinned", pinned_kill)
    audit = MagicMock()
    audit_threads: list[int] = []
    audit.log_tool_invocation.side_effect = lambda **_kwargs: audit_threads.append(
        threading.get_ident()
    )
    event_loop_thread = threading.get_ident()
    monkeypatch.setattr("kiro_crew.run_coordinator.recovery.sel", lambda: audit)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()

    assert report.terminated == 1
    assert calls == [(4321, "same", platform_compat.SIGKILL)]
    assert audit_threads
    assert audit_threads[0] != event_loop_thread
    audit.log_tool_invocation.assert_called_once_with(
        session_key="subagent:live1",
        source="subagent",
        tool_name="orphan_reconcile_kill",
        outcome="kill_authorized",
        metadata={"subagent_id": "live1", "pid": 4321},
        critical=True,
    )


@pytest.mark.asyncio
async def test_recovery_defers_default_termination_without_a_process_identity_pin(
    tmp_path,
    monkeypatch,
):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    pinned_kill = MagicMock(side_effect=AssertionError("POSIX kill must not be attempted"))
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "kill_process_tree_pinned", pinned_kill)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 0
    pinned_kill.assert_not_called()
    assert run is not None
    assert run.observed_state is not ObservedState.TERMINAL


@pytest.mark.asyncio
async def test_recovery_never_terminates_a_shared_runtime(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(
        coordinator,
        clock,
        "shared1",
        4321,
        "",
        process_owned=False,
    )
    terminated: list[int] = []
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=terminated.append,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()

    assert report.terminated == 0
    assert report.interrupted == 1
    assert terminated == []


@pytest.mark.asyncio
async def test_recovery_does_not_terminalize_a_child_that_failed_to_terminate(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")

    def refuse_termination(_pid: int) -> None:
        raise PermissionError("access denied")

    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=refuse_termination,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 0
    assert run is not None
    assert run.observed_state is not ObservedState.TERMINAL


@pytest.mark.asyncio
async def test_recovery_does_not_terminalize_when_pinned_kill_loses_identity(tmp_path):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, "same")
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        terminate_process=lambda _pid: False,
        process_identity=lambda _pid: "same",
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 0
    assert run is not None
    assert run.observed_state is not ObservedState.TERMINAL


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_start,current_start", [("old", "new"), ("old", None)])
async def test_recovery_preserves_live_child_without_verified_identity(
    tmp_path,
    recorded_start,
    current_start,
):
    clock = [100.0]
    coordinator = MemoryRunCoordinator(clock=lambda: clock[0], id_factory=lambda: "event-1")
    await _record_protected_process(coordinator, clock, "live1", 4321, recorded_start)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(
            coordinator,
            AsyncMock(return_value=True),
            owner_id="delivery",
            clock=lambda: clock[0],
        ),
        owner_id="recovery",
        clock=lambda: clock[0],
        process_identity=lambda _pid: current_start,
        process_alive=lambda _pid: True,
    )

    report = await recovery.reconcile()
    run = await coordinator.get_run("live1")

    assert report.interrupted == 0
    assert report.terminated == 0
    assert run is not None
    assert run.observed_state is not ObservedState.TERMINAL


@pytest.mark.asyncio
async def test_legacy_destination_cannot_create_pending_delivery(tmp_path):
    coordinator = MemoryRunCoordinator(id_factory=lambda: "legacy-event")
    folder = _legacy_state(tmp_path, "pending1")
    (folder / "result.txt").write_text("partial", encoding="utf-8")
    _write_json(
        folder / "tombstone.json",
        {
            "id": "pending1",
            "cause": "gateway_restart",
            "recovery_action": "notification_pending",
            "died": 40.0,
            "outcome": "interrupted",
        },
    )
    delivered = AsyncMock(return_value=True)
    recovery = RunRecovery(
        coordinator,
        OutboxDeliveryAdapter(coordinator, delivered, owner_id="delivery"),
        owner_id="recovery",
    )

    report = await recovery.reconcile(importer=LegacyRunImporter(coordinator, root=tmp_path))

    assert report.imported == 1
    assert report.interrupted == 0
    assert report.delivered == 0
    delivered.assert_not_awaited()
    imported = await coordinator.get_run("pending1")
    assert imported is not None
    assert imported.parent_session == ""


@pytest.mark.asyncio
async def test_real_sleeper_is_terminated_after_restart_takeover(tmp_path):
    if not platform_compat.IS_WINDOWS:
        pytest.skip("default identity-pinned termination is available only on Windows")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        start_id = platform_compat.process_start_time(process.pid)
        if start_id is None:
            pytest.skip("process identity is unavailable on this host")
        clock = [100.0]
        coordinator = MemoryRunCoordinator(clock=lambda: clock[0])
        await _record_protected_process(
            coordinator,
            clock,
            "sleeper1",
            process.pid,
            start_id,
        )
        recovery = RunRecovery(
            coordinator,
            OutboxDeliveryAdapter(
                coordinator,
                AsyncMock(return_value=True),
                owner_id="delivery",
            ),
            owner_id="recovery",
            clock=lambda: clock[0],
        )

        report = await recovery.reconcile()
        process.wait(timeout=5)

        assert report.terminated == 1
        run = await coordinator.get_run("sleeper1")
        assert run is not None
        assert run.outcome is RunOutcome.INTERRUPTED
    finally:
        if process.poll() is None:
            try:
                platform_compat.kill_process_tree(process.pid, platform_compat.SIGKILL)
            except (OSError, ProcessLookupError, ValueError):
                pass
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_manager_startup_uses_coordinator_before_legacy_fallback(
    tmp_path,
    monkeypatch,
):
    from kiro_crew.subagent import SubagentManager

    _legacy_state(tmp_path, "startup1")
    (tmp_path / "startup1" / "result.txt").write_text("partial result", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    coordinator = MemoryRunCoordinator(id_factory=lambda: "startup-event")
    announced = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=announced,
        coordinator=coordinator,
    )
    importer = LegacyRunImporter(coordinator, root=tmp_path)
    manager._legacy_run_importer = importer
    manager._run_recovery = RunRecovery(
        coordinator,
        manager._outbox_delivery,
        owner_id="startup-recovery",
    )
    fallback = AsyncMock()
    monkeypatch.setattr(manager, "_reconcile_orphans", fallback)

    await manager._reconcile_startup()

    fallback.assert_not_awaited()
    announced.assert_awaited_once()
    info = announced.await_args.args[0]
    assert info.id == "startup1"
    assert info.error == "interrupted by gateway restart"
    assert info.outcome == "interrupted"
    assert info.result_path == str(tmp_path / "startup1" / "result.txt")
    run = await coordinator.get_run("startup1")
    assert run is not None
    assert run.outcome is RunOutcome.INTERRUPTED


@pytest.mark.asyncio
async def test_manager_startup_fails_closed_when_coordinator_recovery_errors(
    monkeypatch,
) -> None:
    from kiro_crew.subagent import SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    reconcile = AsyncMock(side_effect=OSError("coordinator unavailable"))
    monkeypatch.setattr(manager._run_recovery, "reconcile", reconcile)
    legacy_fallback = AsyncMock()
    monkeypatch.setattr(manager, "_reconcile_orphans", legacy_fallback)

    await manager._reconcile_startup()

    reconcile.assert_awaited_once()
    legacy_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_manager_recovery_excludes_only_active_and_queued_runs() -> None:
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    active = asyncio.create_task(asyncio.Event().wait())
    manager._agents["active"] = SubagentInfo(id="active", task="active")
    manager._agents["retained"] = SubagentInfo(id="retained", task="done", done=True)
    manager._tasks["active"] = active
    manager._scheduler.enqueue({"_preassigned_id": "queued"})
    manager._run_recovery.reconcile = AsyncMock()  # type: ignore[method-assign]

    try:
        await manager._reconcile_startup()
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)

    assert manager._run_recovery.reconcile.await_args.kwargs["exclude_run_ids"] == frozenset(
        {"active", "queued"}
    )
