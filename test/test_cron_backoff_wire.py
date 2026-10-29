"""Failure backoff reaches the `GET /api/crons` wire.

A job with auto-pause off (or a limit above the default) that keeps failing
backs off from its 5th failure in a row, and `compute_next_run_ts` already
returns the shifted time. Without these two fields the Schedule page showed a
later next run with nothing saying why. Mirrors test_cron_retry_wire.py's
harness.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.cron_service.model import _NEVER_PAUSE_BACKOFF_BASE_SECS
from kiro_crew.dashboard.handlers import cron_board
from kiro_crew.dashboard.handlers.cron import api_crons
from kiro_crew.testing.clock import ManualClock

# A fixed stamp: the backoff counts from `last_run_ts`, so the expected epoch is
# exact with no clock read.
_LAST_RUN = 1_770_000_000.0


def _request_with_job(**job_kw: Any) -> MagicMock:
    defaults: dict[str, Any] = dict(
        id="cj1",
        name="failing-job",
        message="",
        schedule=CronSchedule(kind="every", every_secs=60),
        command="false",
        last_status="error",
    )
    defaults.update(job_kw)
    job = CronJob(**defaults)

    state = MagicMock()
    state.crons.list_jobs.return_value = [job]
    state.crons.list_jobs_async = AsyncMock(return_value=[job])
    state.crons.is_running.return_value = False
    state.crons.running_since.return_value = None
    state.has_slot.return_value = False
    request = MagicMock()
    request.app = {"state": state}
    return request


async def _job_payload(**job_kw: Any) -> dict[str, Any]:
    resp = await api_crons(_request_with_job(**job_kw))
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    job: dict[str, Any] = json.loads(resp.body)["jobs"][0]
    return job


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    """The handler's clock, 30 s after ``_LAST_RUN``, on the board module's own binding."""
    manual = ManualClock(start=_LAST_RUN + 30)
    manual.install(monkeypatch, cron_board)
    return manual


_BACKING_OFF: dict[str, Any] = dict(
    auto_pause_after_failures=0, consecutive_failures=5, last_run_ts=_LAST_RUN
)


class TestFailureBackoffOnTheWire:
    @pytest.mark.asyncio
    async def test_a_job_in_backoff_serializes_the_count_and_the_until_epoch(
        self, clock: ManualClock
    ) -> None:
        payload = await _job_payload(**_BACKING_OFF)
        assert payload["consecutive_failures"] == 5
        assert payload["failure_backoff_until"] == _LAST_RUN + _NEVER_PAUSE_BACKOFF_BASE_SECS
        assert payload["next_run_ts"] == payload["failure_backoff_until"]

    @pytest.mark.asyncio
    async def test_a_disabled_job_serializes_null(self, clock: ManualClock) -> None:
        payload = await _job_payload(**_BACKING_OFF, enabled=False)
        assert payload["consecutive_failures"] == 5
        assert payload["failure_backoff_until"] is None

    @pytest.mark.asyncio
    async def test_a_job_whose_next_slot_is_past_the_backoff_serializes_null(
        self, clock: ManualClock
    ) -> None:
        payload = await _job_payload(
            **_BACKING_OFF, schedule=CronSchedule(kind="every", every_secs=86400)
        )
        assert payload["failure_backoff_until"] is None

    @pytest.mark.asyncio
    async def test_an_elapsed_backoff_serializes_null(self, clock: ManualClock) -> None:
        clock.advance(_NEVER_PAUSE_BACKOFF_BASE_SECS + 100)
        payload = await _job_payload(**_BACKING_OFF)
        assert payload["failure_backoff_until"] is None

    @pytest.mark.asyncio
    async def test_a_failing_job_below_the_streak_threshold_serializes_null(self) -> None:
        payload = await _job_payload(
            auto_pause_after_failures=0, consecutive_failures=4, last_run_ts=_LAST_RUN
        )
        assert payload["consecutive_failures"] == 4
        assert payload["failure_backoff_until"] is None

    @pytest.mark.asyncio
    async def test_a_healthy_job_serializes_zero_and_null(self) -> None:
        payload = await _job_payload(last_status="ok", last_run_ts=_LAST_RUN)
        assert payload["consecutive_failures"] == 0
        assert payload["failure_backoff_until"] is None
