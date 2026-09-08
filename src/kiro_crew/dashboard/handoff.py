"""External-agent handoff — "start a trackable Kiro Crew session for me".

An agent running OUTSIDE Kiro Crew (Claude Code, OpenClaw, …) hands a piece of
work over and gets back a session that behaves like every other one: it shows in
the sidebar, keeps its history, resumes, and is governed. Upstream asks for this
in two halves — kirodotdev/KiroCrew#7697 (an entry point an external agent can
call) and #7797 (finding non-dashboard sessions in the dashboard) — and neither
has an implementation.

This module is the single chokepoint. There is more than one caller (the HTTP
endpoint and the ``session_handoff`` MCP tool) and they must share ONE decision,
not each grow their own — the same reason ``publish_governance`` exists.

**What the caller does NOT get.** A handed-over session gains no approval
authority. It starts a turn and runs under the grants the operator already made
(slot trust, YOLO) and stops at the first tool those do not cover, exactly like
any other unattended slot. Auto-approval sources stay slot trust or YOLO only;
this must never become a third. The slot is created APP-OWNED
(``_app = "handoff:<origin>"``), which is what puts it on the unattended path:
the deny-fast approval window applies (nobody is watching at that moment) and
the turn is charged against the background-turn cap. Both are deliberate — see
``test_unattended_slot_guardrails`` for why an unattended slot needs each.

**Why an operator opt-in gates it.** The transport already admits any local
process running as this user: the gateway secret is readable by all of them, so
this endpoint grants no new transport reach. But "a local process may read my
sessions" and "a local process may start an autonomous agent run on my machine"
are different consents, and installing a tool server should not silently give
the second. So the START half is gated on the keystone
``workflow_policy.allow_external_handoff``, default OFF, which the agent can
neither read nor write.

Disposition on error: fails CLOSED. A handoff is an authorization decision (a
turn executes on the host), so an unreadable ceiling or an unknown origin refuses
rather than degrading to permit.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from kiro_crew import sel as _sel_mod
from kiro_crew import workflow_policy
from kiro_crew.security import is_sensitive_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Origins a handoff may declare. A CLOSED set, matched by exact membership:
#: the value is persisted on the slot, rendered in the sidebar, and interpolated
#: into the provenance line the model reads, so free text here would be
#: caller-authored content on three surfaces at once. An unknown origin refuses.
KNOWN_ORIGINS: frozenset[str] = frozenset({"claude_code", "openclaw", "codex", "external"})

#: Prefix of the ``_app`` owner tag. Anything under it is app-owned and therefore
#: unattended; the suffix names which external tool handed the work over.
APP_TAG_PREFIX = "handoff:"

#: Provenance stamped onto the handed-over prompt. The model must not read a
#: handoff as the operator typing: it arrived from automation, and the injected
#: -messages rule applies. Mirrors ``session_control._SEND_PROVENANCE``.
HANDOFF_PROVENANCE = "[handed over by {origin} — the user may not be present]\n\n"

#: Longest prompt a handoff may carry. A handoff is a work item, not a transcript
#: transfer; a caller with more context to move should write it to a file and
#: reference the path, which is also what keeps it out of the session log.
MAX_PROMPT_CHARS = 16_000


class HandoffRefused(Exception):
    """A handoff was refused. ``code`` is the machine-readable reason."""

    def __init__(self, code: str, message: str, *, status: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def app_tag(origin: str) -> str:
    """The ``_app`` owner tag for a handoff from *origin*."""
    return f"{APP_TAG_PREFIX}{origin}"


def origin_of(app: str) -> str:
    """The origin a handoff-owned slot came from, or ``""`` for any other slot.

    Read rather than parsed at the call sites so the tag format lives in one
    place; a non-handoff ``_app`` (an ordinary App Kit app) answers ``""``.
    """
    if not isinstance(app, str) or not app.startswith(APP_TAG_PREFIX):
        return ""
    origin = app[len(APP_TAG_PREFIX) :]
    return origin if origin in KNOWN_ORIGINS else ""


def _audit(*, origin: str, outcome: str, reason: str, slot: str = "") -> None:
    """Record one handoff decision on the security event log; never raise.

    Both directions are audited, not just refusals: "when did an external agent
    start a run on this host" is the question this feature creates, and an
    operator reconstructing it must find the permits too.
    """
    try:
        _sel_mod.sel().log_tool_invocation(
            session_key=slot,
            agent="kirocrew",
            source="handoff",
            tool_name="session_handoff",
            tool_kind="session",
            outcome=outcome,
            metadata={"origin": origin, "reason": reason},
        )
    except Exception:
        logger.debug("handoff audit failed", exc_info=True)


def _vet(origin: str, prompt: str) -> str:
    """Validate the request and confirm the operator permits it.

    Returns the validated origin. Raises :class:`HandoffRefused` otherwise.

    Order matters: the origin is validated BEFORE the ceiling is read, so a
    refusal names the caller's own mistake rather than blaming the operator's
    configuration for a malformed request.
    """
    if not isinstance(origin, str) or origin not in KNOWN_ORIGINS:
        _audit(origin=str(origin)[:40], outcome="denied", reason="unknown_origin")
        raise HandoffRefused(
            "unknown_origin",
            f"origin must be one of {sorted(KNOWN_ORIGINS)}",
            status=400,
        )
    if not isinstance(prompt, str) or not prompt.strip():
        _audit(origin=origin, outcome="denied", reason="empty_prompt")
        raise HandoffRefused("empty_prompt", "prompt must be a non-empty string", status=400)
    if len(prompt) > MAX_PROMPT_CHARS:
        _audit(origin=origin, outcome="denied", reason="prompt_too_long")
        raise HandoffRefused(
            "prompt_too_long",
            f"prompt exceeds {MAX_PROMPT_CHARS} characters; reference a file instead",
            status=400,
        )
    try:
        permitted = workflow_policy.allow_external_handoff()
    except Exception:
        # Fails CLOSED, and loudly: an unreadable ceiling must not authorise an
        # autonomous run. workflow_policy already fails soft to False, so
        # reaching here means something worse than a missing file.
        logger.warning("handoff ceiling unreadable; refusing", exc_info=True)
        permitted = False
    if not permitted:
        _audit(origin=origin, outcome="denied", reason="not_permitted")
        raise HandoffRefused(
            "handoff_not_permitted",
            "external session handoff is off. Turn it on in Settings > Security > Rules "
            "(it lets any local process start an unattended run on this host).",
        )
    return origin


def _resolve_project(project: str) -> str:
    """Resolve and vet the working directory for the new session.

    Same three checks the dashboard's own slot-create applies, in the same order
    — realpath, is-a-directory, not sensitive — because a handoff must not reach
    a directory the dashboard would refuse to open. ``""`` means "no project",
    which the slot resolves the ordinary way.
    """
    if not project:
        return ""
    if not isinstance(project, str):
        raise HandoffRefused("bad_project", "project must be a string", status=400)
    resolved = os.path.realpath(os.path.expanduser(project.strip()))
    if not os.path.isdir(resolved):
        raise HandoffRefused("project_not_a_directory", f"not a directory: {project}", status=400)
    if is_sensitive_path(resolved):
        raise HandoffRefused("project_denied", "access denied", status=403)
    return resolved


async def start_handoff(
    state: "DashboardState",
    *,
    origin: str,
    prompt: str,
    project: str = "",
    agent: str = "",
    model: str = "",
    title: str = "",
    start: bool = True,
) -> dict[str, Any]:
    """Create a trackable session for *prompt* and (by default) start its turn.

    Returns ``{"slot", "origin", "started", "queued", "project"}``. ``started``
    and ``queued`` are reported separately for the same reason
    ``session_control.send_to_target`` distinguishes them: "it is running" and
    "it will run when the slot frees" must not look identical to a caller that
    is about to poll for a result.

    The turn is launched as a tracked background task and this returns without
    waiting for it — an external caller must not hold an HTTP request open for
    the length of an agent turn. Progress is read back the ordinary way (the
    dashboard, or the read-only session tools).
    """
    # Imported here, not at module scope: chat_runner imports the dashboard
    # state module, which imports this one for the sidebar's origin label.
    from kiro_crew.dashboard.chat_runner import _run_chat
    from kiro_crew.dashboard.state import SlotOrigin
    from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

    origin = _vet(origin, prompt)
    resolved_project = _resolve_project(project)

    slot = state.get_or_create_slot(
        name=title.strip() or None,
        agent=agent or "",
        model=model or "",
        # App-owned, which is what makes it unattended: the deny-fast approval
        # window applies and the turn is charged against the background-turn
        # cap. A person opening it flips ``_human_seen`` and it behaves as an
        # attended session from then on.
        app=app_tag(origin),
        origin=SlotOrigin.APP,
    )
    if resolved_project:
        slot.project = resolved_project

    message = HANDOFF_PROVENANCE.format(origin=origin) + prompt.strip()

    started = False
    queued = False
    if start:
        # The same queue-vs-run decision the composer makes, then the same two
        # wrappers the app-owned path in ``api_chat`` uses: the background-turn
        # cap (inert for an attended slot, binding for this one) inside the
        # guarded-turn spawn that tracks the task and logs its exceptions.
        if slot.running:
            slot.queue_append(message)
            queued = True
        else:
            slot.append("user", message, "msg msg-u")
            spawn_guarded_turn(
                state,
                slot,
                state.run_background_turn(
                    slot,
                    _run_chat(state, slot, message, _directive_user_origin=False),
                ),
            )
            started = True

    _audit(
        origin=origin,
        outcome="ok",
        reason=("started" if started else "queued" if queued else "created"),
        slot=slot.key,
    )
    logger.info(
        "handoff from %s -> slot %s (started=%s queued=%s project=%s)",
        origin,
        slot.key,
        started,
        queued,
        resolved_project or "-",
    )
    return {
        "slot": slot.key,
        "origin": origin,
        "started": started,
        "queued": queued,
        "project": resolved_project,
    }
