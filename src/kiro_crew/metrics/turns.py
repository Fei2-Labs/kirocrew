"""``kirocrew.turn.duration`` — one histogram, emitted for every surface.

The instrument powers two readings on the Telemetry page: turn latency
(p50/p90) and fault rate (the share of turns whose outcome is not ``ok``).
Both are only as honest as the population they sample, which is why this
module exists at all.

**Why this is not in ``chat_runner``.** It used to be. The emit lived beside
the dashboard turn loop, which made it structurally reachable from exactly one
surface: a cron job, a heartbeat task, a memory consolidation pass, a subagent,
a task-runner step, a workflow stage and every messaging channel each run agent
turns that never pass through that loop, so none of them produced a sample. The
consequence is worse than a gap, because an absent sample does not read as
absent: a background surface that is slow, stalling or erroring contributed
nothing, so the page rendered the interactive median and called it the system's
health. There was also no way to see how many background turns had run at all.

**The shared boundary is the usage row, not the ACP turn.** The obvious
candidate was ``acp/session_handle.py::SessionHandle._run_turn``, which every
ACP-backed surface really does cross. It was rejected: the handle knows only its
ACP ``sessionId``, not the Kiro Crew session key this metric must group by, and
an ``AcpClient``-backed session (the claude_code backend) never reaches it at
all. ``persist_token_record_async`` is the boundary that actually fits — every
surface already calls it once per turn at ``EVENT_COMPLETE``, and it already
receives the session key, the measured ``elapsed_ms`` and the turn's usage. So
the metric is emitted where the row is written, and the two can never disagree
about one turn.

**Outcome is passed in, never guessed.** The ``event`` argument those callers
pass is heterogeneous (an ``LLMEvent`` from the chat path, a bare ``TurnUsage``
from the helper sites), so a stop reason cannot be read off it reliably. Each
surface states its own outcome instead, and ``test_turn_duration_recorded.py``
holds every call site to that. Defaulting to ``ok`` would have been the harmful
alternative: it would bury exactly the failing background turns the metric is
being widened to expose.
"""

from __future__ import annotations

import logging

from kiro_crew.acp.types import STOP_REASON_STALE_RECOVER, STOP_REASON_TOOL_STALL
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.metrics.provider import get_recorder

logger = logging.getLogger(__name__)

#: The instrument name. ``dashboard/handlers/telemetry.py`` reads the same
#: constant, so emitter and reader cannot drift apart.
#:
#: ``metrics/provider.py``'s bucket table keeps its own literal, and that is not
#: an oversight: this module imports ``get_recorder`` from it, so having it import
#: the name back would be a cycle. That one duplicate is the cost of the import
#: direction, not a spelling nobody noticed.
TURN_METRIC = "kirocrew.turn.duration"

#: Outcome for a turn whose surface could not determine a stop reason at all.
#: Deliberately NOT ``unknown``: that label is in
#: ``telemetry._TERMINAL_FAULT_OUTCOMES``, so every clean background turn
#: reaching a helper site that passes a bare ``TurnUsage`` would have landed in
#: the fault-rate NUMERATOR and inflated the fault rate the moment this metric
#: was widened. It is also not folded into ``ok``, which would claim a success
#: nobody observed. It is its own slice, excluded from faults and visible in the
#: outcome breakdown, so the blind spot is a number an operator can see rather
#: than a guess in either direction.
OUTCOME_UNCLASSIFIED = "unclassified"


def turn_outcome(stop_reason: str | None, *, exhausted: bool = False) -> str:
    """Map a turn's stop reason to a low-cardinality outcome label.

    Single source of truth shared by every surface's emit and by the unit tests,
    so the mapping cannot drift from what the tests assert. Every label this
    metric can carry is returned from HERE, which is what lets
    ``test_telemetry_handler``'s drift gate harvest them by AST and prove each
    one is classified by the fault aggregator.

    ``None`` and ``""`` both mean a clean turn: the acp path leaves
    ``event.stop_reason`` unset on a normal completion, so this function must not
    read absence as failure. "This surface had no stop reason to GIVE" is a
    different statement, and it is not expressible here — a caller holding a bare
    ``TurnUsage`` cannot distinguish its missing attribute from a clean ``None``
    at this layer, so it passes :data:`OUTCOME_UNCLASSIFIED` itself.

    The two watchdog stop reasons are distinct outcomes, not ``error``: a
    stall-recovery turn is re-driven in place (its budget/outcome is tracked by
    ``kirocrew.watchdog.recovery.outcome``), so folding it into ``error`` would
    make the fault rate count every recovered stall as a fault AND hide the
    stall population the watchdog work exists to measure. Checked BEFORE the
    ``timeout`` substring so a stall never misclassifies.

    ``exhausted`` marks a stall turn whose recovery budget is already spent: the
    slot dies with "start a new chat", so the turn labels ``stall_exhausted`` — a
    terminal fault to the aggregator — keeping the recovered-stall exclusion
    from hiding dead sessions while ``fault_rate`` stays a single-series
    computation. Only the dashboard turn loop maintains such a budget; a
    background surface has no recovery loop, so it never passes this.
    """
    s = stop_reason or ""
    if s in ("", "end_turn", "stop", "completed"):
        return "ok"
    if s == STOP_REASON_TOOL_STALL or s == STOP_REASON_STALE_RECOVER:
        if exhausted:
            return "stall_exhausted"
        return "tool_stall" if s == STOP_REASON_TOOL_STALL else "stale_recover"
    if "timeout" in s:
        return "timeout"
    return "error"


def emit_turn_duration(
    duration_ms: int | float | None,
    *,
    session_key: str,
    outcome: str,
    elapsed_ms: int | float | None = None,
) -> None:
    """Emit one ``kirocrew.turn.duration`` sample (best-effort, never raises).

    ``duration_ms`` is the provider-reported duration and ``elapsed_ms`` the
    locally measured wall clock; the first non-zero wins. Both are needed
    because the acp provider ALWAYS reports ``TurnUsage.duration_ms == 0``
    (nothing in the codebase assigns it — only claude_code fills it in), so a
    provider-only value silently skipped the emit for effectively all traffic
    and left turn latency / fault rate / throughput reading a flat 0.

    A still-zero duration skips the emit deliberately: an absent sample reads as
    "no data" on the Telemetry page, whereas a recorded 0 would render as a
    plausible-looking 0ms p50 — the very symptom that guard's misuse caused.

    ``session_source`` is derived with
    :func:`kiro_crew.messaging.link.telemetry_channel_of`, which exists for
    exactly this question ("who paid this cost") and returns a bounded label:
    the transport namespace for a channel key, a local label for the rest,
    ``other`` for a shape it does not recognise. Deliberately NOT
    ``validation.infer_use_case``, whose output also gates an artifact check
    (``handlers/artifacts.py``) — a metric must not be the reason an
    authorization-adjacent mapping grows a case.

    Caveat on what the wall clock measures: ``elapsed_ms`` runs from the start
    of the turn, so a turn parked on an interactive tool-approval prompt counts
    the operator's thinking time as turn duration. There is no finer-grained
    source on the acp path (the provider reports nothing at all), so this is the
    honest maximum available — but it means the histogram is "turn wall-clock",
    not pure model latency, and a high p90 can mean slow approvals rather than a
    slow model.
    """
    value = duration_ms or elapsed_ms
    if not value:
        return
    attrs: dict = {"outcome": outcome or OUTCOME_UNCLASSIFIED}
    try:
        source = telemetry_channel_of(session_key)
        if source:
            attrs["session_source"] = source
    except Exception:
        pass
    try:
        get_recorder().histogram(TURN_METRIC, value, unit="ms", attrs=attrs)
    except Exception:
        logger.debug("turn metric emit failed", exc_info=True)
