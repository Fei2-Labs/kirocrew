"""Stats from the last ACP prompt (``AcpPromptStats``), split out of
``types.py`` for the file-line cap. Every name here is re-exported by
``kiro_crew.acp.types``; keep importing from that path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.acp.types import RefusalInfo, TurnUsage


logger = logging.getLogger(__name__)


@dataclass
class AcpPromptStats:
    """Stats from the last ACP prompt."""

    event_count: int = 0
    text_chunks: int = 0
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    context_pct: float = 0.0
    # Raw token counts from the adapter's usage_update {used, size}. context_pct
    # is derived as used/size*100, but the dashboard token TEXT must use the
    # real served window (size) — re-deriving it on the frontend from the model
    # id (e.g. assuming 1M for "[1m]") can disagree with the window the adapter
    # actually divided by, inflating the displayed "X / Y tokens". 0 = unknown.
    context_used_tokens: int = 0
    context_window_tokens: int = 0
    # True once a real ``usage_update {used, size}`` has set the token counts
    # above. When set, those counts (and the ``context_pct`` derived from them)
    # are AUTHORITATIVE: kiro's separately-streamed ``_kiro.dev/metadata``
    # ``contextUsagePercentage`` must NOT overwrite ``context_pct`` (it can
    # disagree with used/size — measuring a different window — which would make
    # the dashboard show a headline % inconsistent with the "used / total"
    # token text). Also gates the pct-only ``_backfill_context_window`` so a
    # registry-derived window never clobbers real served counts. Defaults False
    # and re-inits per turn, carried across turns alongside the counts.
    context_tokens_from_usage: bool = False
    # Per-turn billing credits summed from kiro's _kiro.dev/metadata
    # meteringUsage (unit="credit"). 0 for providers that bill in tokens.
    credits: float = 0.0
    # Per-turn token counts from the PromptResponse (the claude-agent-acp
    # adapter reports them there; kiro-cli's response carries none, so these
    # stay 0 on the kiro path). Accumulated like ``credits``; reset per turn
    # by ``carry_over()``.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Per-turn cost delta in USD, derived from the adapter's session-cumulative
    # ``usage_update.cost.amount`` via ``apply_cost_cumulative``. Reset per turn
    # by ``carry_over()``.
    cost_usd: float = 0.0
    # Last seen session-cumulative cost — the baseline the next reading is
    # delta'd against. SESSION state: survives ``carry_over()`` like the
    # context fields, or every turn would re-bill the whole session's spend.
    cost_session_usd: float = 0.0
    # The ACP session ``cost_session_usd`` was measured on. Lets a restore of
    # that SAME session keep the baseline (see ``restore_cost_session``).
    cost_baseline_sid: str = ""
    # True while the adapter's counter holds spend Crew never saw: a restored
    # session's ledger is brought back from the harness's own transcript, so
    # its first reading includes everything billed before this process. The
    # next reading is adopted as the baseline and bills nothing.
    cost_baseline_unknown: bool = False
    # True while ``context_pct`` reads 0.0 only because a compaction dropped the
    # counts and no fresh telemetry has re-derived them. Distinguishes "the
    # transcript is empty" from "the transcript's size is unknown" — the two are
    # indistinguishable by value, and a consumer that reads the second as the
    # first sees a session that just hit its context ceiling as brand new.
    # Cleared the moment a real percentage or usage_update lands.
    context_pct_unknown: bool = False
    # The structured refusal this turn's metadata reported, if any. Written by
    # the ``_kiro.dev/metadata`` tracker when the notification carries a
    # ``refusal`` payload (``ACP_BACKENDS_STRUCTURED_REFUSAL``), read by the
    # ``EVENT_COMPLETE`` builder to fold onto the terminal. PER-TURN: the
    # notification precedes the terminal by milliseconds and describes only
    # this turn, so ``carry_over()`` drops it -- a refusal that survived into
    # the next turn would brand an ordinary answer as declined.
    refusal: "RefusalInfo | None" = None
    # The KAS engine's own measurement of this turn (``kas_profile`` module): the
    # context breakdown and time to first token. PER-TURN, dropped by
    # ``carry_over()``, so a replayed or late frame cannot reach a later turn.
    kas_profile: "dict[str, Any] | None" = None
    # The context-window occupancy, in tokens, after each model call this turn,
    # kept as a running sum, the number of readings and the first reading (see
    # ``note_metadata_frame``). Fixed size however many calls the turn makes.
    # PER-TURN: ``carry_over()`` drops all three.
    call_context_sum: int = 0
    call_readings: int = 0
    first_call_reading: int = 0
    # Credit entries the turn's ``meteringUsage`` listed. kiro-cli lists one per
    # model call, which is how ``to_turn_usage`` tells a reading that belongs to
    # no call (the frame sent when a session opens) from one that does.
    # PER-TURN like ``credits``.
    metered_calls: int = 0

    def carry_over(self) -> "AcpPromptStats":
        """Return fresh per-turn stats carrying this turn's context state.

        Event/tool/credit counters are per-turn and start at zero; the context
        state describes the SESSION and must survive the re-init, or every turn
        boundary would re-report an empty context.
        """
        return AcpPromptStats(
            context_pct=self.context_pct,
            context_used_tokens=self.context_used_tokens,
            context_window_tokens=self.context_window_tokens,
            context_tokens_from_usage=self.context_tokens_from_usage,
            context_pct_unknown=self.context_pct_unknown,
            cost_session_usd=self.cost_session_usd,
            cost_baseline_sid=self.cost_baseline_sid,
            cost_baseline_unknown=self.cost_baseline_unknown,
        )

    def reset_context_state(self) -> None:
        """Drop ALL context state when the runtime is re-bound to a new session.

        The inverse commitment of :meth:`carry_over`: that method preserves the
        context fields because they describe the SESSION — which is exactly why
        they must NOT survive a warm-pool handoff, where the runtime outlives
        whatever it did before the re-bind. Stale stats handed to a new chat
        make ``check_context_usage`` fire compaction on an empty conversation.

        Everything returns to dataclass defaults, window included: a handoff
        may re-apply a different model post-claim, and a window measured before
        the re-bind has no claim to describe the next session.

        ``context_pct_unknown`` deliberately resets to ``False``, NOT ``True``:
        the claimed runtime serves a fresh, never-prompted ``session/new``, so
        "confirmed empty" is the accurate reading. Flagging it unknown would
        collide with the flag's existing meaning — "the backend compacted this
        session in place" — which the background-session recycle decision reads
        as a recycle-now signal (``pct == 0.0 and unknown``); a just-claimed
        provider must not match that predicate.
        """
        self.context_pct = 0.0
        self.context_used_tokens = 0
        self.context_window_tokens = 0
        self.context_tokens_from_usage = False
        self.context_pct_unknown = False
        # The adapter's cumulative cost counter belongs to the OLD session; the
        # fresh session/new starts it at zero, so a kept baseline would
        # under-count the first turns (any new reading below the stale baseline
        # only survives via the monotonic reset guard).
        self.cost_session_usd = 0.0
        self.cost_baseline_unknown = False

    def note_pct_reported(self) -> None:
        """Mark ``context_pct`` as backed by real telemetry.

        Called wherever a percentage or usage_update is applied, so a zero that
        follows a compaction stops reading as "unknown" once the backend says
        what the compacted transcript actually costs.
        """
        self.context_pct_unknown = False

    def terminal_refusal(self, stop_reason: str) -> tuple[str, "RefusalInfo | None"]:
        """Fold this turn's refusal evidence onto a terminal's stop reason.

        A structured refusal recorded from metadata wins: the terminal's own
        reason is unreliable there (Kiro reports ``end_turn`` after streaming
        the canned explanation as text), so the reason is rewritten to
        ``STOP_REASON_REFUSAL`` and the payload attached. Every other reason --
        including a bare ``refusal`` (Anthropic's spelling, passed through by
        every harness) -- is returned untouched with ``None``: the dashboard's
        refusal branch keys on the stop reason, and its card renders ``None``
        as the field-less card, so a payload with nothing in it would add
        nothing.
        """
        if self.refusal is not None:
            from kiro_crew.acp.types import STOP_REASON_REFUSAL

            return STOP_REASON_REFUSAL, self.refusal
        return stop_reason, None

    def begin_cost_session(self, session_id: str) -> None:
        """Start the cost baseline for a session this process just created.

        A ``session/new`` opens a fresh ledger in the adapter, so its counter
        starts at zero and every later reading is spend Crew can see.
        """
        self.cost_session_usd = 0.0
        self.cost_baseline_sid = session_id or ""
        self.cost_baseline_unknown = False

    def restore_cost_session(self, session_id: str) -> None:
        """Set the cost baseline for a session restored by load or resume.

        The claude harness restores a resumed session's cost ledger from its
        own transcript, so the counter does NOT restart at zero: it carries the
        session's whole lifetime spend. A baseline measured on this same session
        (the adapter process restarted under a live client) still describes
        that ledger and is kept. Any other baseline -- a fresh client after a
        gateway restart, or one measured on a different session -- cannot, so
        the next reading is adopted as the baseline instead of billed.
        """
        sid = session_id or ""
        if sid and sid == self.cost_baseline_sid and not self.cost_baseline_unknown:
            return
        self.cost_session_usd = 0.0
        self.cost_baseline_sid = sid
        self.cost_baseline_unknown = True

    def apply_cost_cumulative(self, cumulative: float) -> None:
        """Fold a session-cumulative cost reading into the per-turn delta.

        The adapter reports cost as a running session total, so the per-turn
        figure is the movement since the last reading. Two readings carry spend
        Crew cannot attribute, and both bill nothing and rebase:

        - the first reading after ``restore_cost_session`` found no usable
          baseline: it includes spend billed before this process;
        - a reading BELOW the stored baseline: the ledger was restored to a
          total Crew never saw, so taking it whole would re-bill the lifetime.

        Under-counting one turn is bounded; re-billing a session's lifetime is
        not. Each case logs a WARNING so a gap in the usage rows explains
        itself. The caller validates the value (finite, non-negative) at the
        ``parse_usage_cost`` chokepoint.
        """
        if self.cost_baseline_unknown:
            logger.warning(
                "cost baseline unknown for restored session %s: adopting "
                "cumulative $%.6f as the baseline; this reading bills $0",
                self.cost_baseline_sid or "(unknown)",
                cumulative,
            )
            self.cost_baseline_unknown = False
            self.cost_session_usd = cumulative
            return
        delta = cumulative - self.cost_session_usd
        if delta < 0:
            logger.warning(
                "cost cumulative for session %s fell from $%.6f to $%.6f: "
                "rebasing; this reading bills $0",
                self.cost_baseline_sid or "(unknown)",
                self.cost_session_usd,
                cumulative,
            )
            delta = 0.0
        self.cost_usd += delta
        self.cost_session_usd = cumulative

    def apply_prompt_token_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
    ) -> None:
        """Accumulate the PromptResponse's turn-scoped token counts.

        Accumulated (not assigned) to mirror ``credits``: the counters are
        per-turn and ``carry_over()`` zeroes them at the turn boundary, so a
        turn that sees one response reads identically either way, and one that
        sees several sums them. Callers validate at the
        ``parse_prompt_token_usage`` chokepoint.
        """
        self.input_tokens += int(input_tokens)
        self.output_tokens += int(output_tokens)
        self.cache_read_tokens += int(cache_read_tokens)
        self.cache_write_tokens += int(cache_write_tokens)

    def note_metadata_frame(self, pct: float | None, metering: object) -> None:
        """Record what one ``_kiro.dev/metadata`` frame says about the turn's calls.

        kiro-cli sends one frame after every model call, carrying the
        ``contextUsagePercentage`` that call left, and closes the turn with a
        frame whose ``meteringUsage`` lists one credit entry per call and
        repeats the last percentage. So a frame with metering is counted by its
        credit entries and its percentage is not recorded a second time; any
        other frame records one call's context in tokens.

        ``pct`` is the caller's sanitized percentage, ``None`` when the frame had
        none. A reading needs a known window, so with the window unknown the
        turn still counts its metered calls but sums no context. A real
        ``usage_update`` makes the meter authoritative and the percentage is not
        applied, so nothing is recorded from it either. Shared by the
        ``AcpClient`` and ``AcpSessionHandle`` metadata trackers, called after
        the percentage has been applied, so the two paths cannot drift.
        """
        if isinstance(metering, list):
            self.metered_calls += sum(
                1 for entry in metering if isinstance(entry, dict) and entry.get("unit") == "credit"
            )
            return
        if pct is None or self.context_tokens_from_usage:
            return
        if self.context_window_tokens > 0:
            reading = round(self.context_window_tokens * pct / 100.0)
            if self.call_readings == 0:
                self.first_call_reading = reading
            self.call_context_sum += reading
            self.call_readings += 1

    def to_turn_usage(self) -> "TurnUsage":
        """One ``TurnUsage`` carrying every billing dimension this turn filled.

        The single source of truth for stats → event conversion: every
        ``EVENT_COMPLETE`` construction site uses this instead of hand-filling
        fields, so a backend that bills in cost/tokens (the claude seam) and
        one that bills in credits (kiro) both surface whatever they reported.
        A backend that sends neither cost, token counts nor per-call metadata
        leaves those dimensions at their zero defaults, so the result is
        byte-identical to ``TurnUsage(credits=...)`` (harness parity).

        The model-call count and context sum come from ``note_metadata_frame``.
        A turn that recorded exactly one reading more than it metered calls
        leaves out its first reading: that is the frame a session sends when it
        opens, which arrives ahead of the first call and measures no call. Any
        other mismatch keeps every reading, and so does a turn whose metering
        frame never arrived (a cancelled first turn whose frame missed the
        bounded read after its answer then counts the session-open reading). A
        turn that compacts mid-way records kiro-cli's post-compaction report
        like a call's reading, so its sum is approximate.
        """
        from kiro_crew.acp.types import TurnUsage

        context_sum = self.call_context_sum
        readings = self.call_readings
        if self.metered_calls and readings == self.metered_calls + 1:
            context_sum -= self.first_call_reading
            readings -= 1
        return TurnUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            # Anthropic's "cache write" is a cache-creation charge.
            cache_creation_tokens=self.cache_write_tokens,
            cost_usd=self.cost_usd,
            credits=self.credits,
            num_turns=self.metered_calls or readings,
            context_sum_tokens=context_sum,
        )

    @staticmethod
    def sanitize_pct(value: object) -> float | None:
        """Coerce a raw context-usage percentage to a real [0, 100] float.

        Both the kiro-cli ``contextUsagePercentage`` and the KAS
        ``usagePercentage`` fields feed this. Returns ``None`` for a missing or
        unparseable value (the caller leaves the meter untouched). A malformed
        number (NaN, ±inf, or a huge finite like 1e308) is clamped — NaN via its
        self-inequality — so ``context_pct`` is always valid JSON and never
        overflows the downstream ``round(win * pct / 100)``.
        """
        if value is None:
            return None
        try:
            pct = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            # OverflowError: a JSON integer beyond float range — malformed
            # telemetry must degrade to "absent", never abort the active turn.
            return None
        return 0.0 if pct != pct else min(max(pct, 0.0), 100.0)

    def backfill_context_window(self, pct: float, model_id: str) -> None:
        """Derive window/used tokens from the model registry when only a
        percentage is available.

        kiro-cli 2.10+ metadata and KAS ``context_usage`` both give a percentage
        with no ``usage_update {used, size}``. Shared by the AcpClient and
        AcpSessionHandle paths so both report the same context-meter token
        counts. No-op once a real usage_update has
        set authoritative counts. ``model_id`` is the caller's resolved id (the
        kiro-agent ``currentModelId``, else the user-picked alias). Resolves the
        window through ``model_registry.model_window`` (operator window > kiro-list
        cache > registry > heuristic) and only backfills a KNOWN window, leaving 0 for a
        genuinely-unknown model so the frontend's own authoritative window drives
        the meter. A real ``usage_update.size`` always wins. A surviving
        ``context_window_tokens`` (e.g. kept across a compaction reset — the
        model did not change) outranks the registry, since the served size can
        differ from the static entry.
        """
        if self.context_tokens_from_usage:
            return  # a real usage_update already set authoritative counts
        win = self.context_window_tokens
        if not win or win <= 0:
            if not model_id:
                return
            # Deferred import: model_registry is a leaf module, but importing it
            # at module scope would drag it into the very early types import.
            from kiro_crew import model_registry

            if not model_registry.has_known_window(model_id):
                return
            reg_win = model_registry.model_window(model_id)
            if not reg_win or reg_win <= 0:
                return
            win = int(reg_win)
            self.context_window_tokens = win
        # sanitize_pct already clamps live telemetry, but a caller may pass a raw
        # pct here; guard the multiply so a stray NaN/inf can never overflow.
        safe_pct = 0.0 if pct != pct else min(max(pct, 0.0), 100.0)
        self.context_used_tokens = round(win * safe_pct / 100.0)

    def reset_after_compaction(self) -> None:
        """Drop the usage counts after a successful compaction.

        The compacted transcript's true size is unknown until the next turn's
        telemetry reports it, and the pre-compaction counts no longer describe
        the session. Keeping them would re-broadcast a stale meter, and —
        worse — a stale ``context_tokens_from_usage=True`` gates
        ``_track_metadata`` / ``_backfill_context_window``, so even a fresh
        post-compaction metadata percentage could never correct it. The window
        is kept: the model did not change, so the served window still holds.

        The zeroed ``context_pct`` is flagged unknown, not empty: a consumer
        that recycles or compacts on a threshold would otherwise read a session
        sitting at its ceiling as freshly started and leave it in place, paying
        the backend's own auto-compaction over and over.
        """
        self.context_tokens_from_usage = False
        self.context_used_tokens = 0
        self.context_pct = 0.0
        self.context_pct_unknown = True

    def rebase_to_window(self, window_tokens: int) -> None:
        """Re-anchor the token stats to a new model's context window.

        Called after a mid-session ``session/set_model``: the previous model's
        window no longer describes the session, and ``context_tokens_from_usage``
        must drop so the next metadata percentage can re-derive against the new
        model (a stale True gates ``_backfill_context_window`` forever when the
        new model streams only ``contextUsagePercentage``). ``context_used_tokens``
        is kept — the transcript is unchanged and token counts are roughly
        model-independent — and ``context_pct`` is recomputed against the new
        window when it is known. Pass 0 for an unknown window: window AND pct
        zero out (the old model's pct must not ship in the reset broadcast),
        so downstream consumers fall back to their own model-derived value
        until the next turn's telemetry re-derives real numbers.
        """
        self.context_tokens_from_usage = False
        if window_tokens and window_tokens > 0:
            self.context_window_tokens = int(window_tokens)
            if self.context_used_tokens > 0:
                pct = self.context_used_tokens / window_tokens * 100.0
                self.context_pct = round(min(max(pct, 0.0), 100.0), 1)
        else:
            self.context_window_tokens = 0
            self.context_pct = 0.0


