"""Manager compatibility wiring for extracted lifecycle boundaries."""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_lifecycle import SubagentLifecycle
from kiro_crew.subagent_scheduler import SubagentScheduler


def test_manager_owns_scheduler_and_lifecycle_boundaries() -> None:
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        max_concurrent=4,
    )

    assert isinstance(manager._scheduler, SubagentScheduler)
    assert manager._scheduler.max_concurrent == 4
    assert isinstance(manager._lifecycle, SubagentLifecycle)
    assert manager._queue is manager._scheduler.queue
    assert manager._report_tasks is manager._lifecycle.report_tasks
    assert manager._teardown_gates is manager._lifecycle.teardown_gates


def test_manager_terminal_claims_and_slot_accounting_delegate() -> None:
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        max_concurrent=1,
    )
    info = SubagentInfo(id="run-1", task="test")
    manager._scheduler.occupy(info, now=10.0)

    assert manager._claim_finalize(info) is True
    assert manager._claim_finalize(info) is False
    # The private wrapper retains its historical claim-only contract for
    # integrations; production release-and-decrement lives on the scheduler.
    assert manager._release_slot(info) is True
    assert manager._release_slot(info) is False
    assert manager.running_count == 1
