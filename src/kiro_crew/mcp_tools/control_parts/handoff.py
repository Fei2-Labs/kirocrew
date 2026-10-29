"""The external-agent handoff tool: ``session_handoff``.

A tool for agents running OUTSIDE Kiro Crew (Claude Code, OpenClaw, Codex) that
hands a piece of work over as a NEW trackable session. Every decision lives
behind ``POST /api/chat/handoff`` in ``dashboard.handoff`` — the operator
opt-in, the closed origin set, the project vetting, the unattended wiring, the
audit — so this tool and the HTTP route cannot drift apart.

This module is an owner of :mod:`kiro_crew.mcp_tools.control`, which serves every name
here and stays the import path and the patch seam: a patch through it reaches this module
too (:mod:`kiro_crew._facade_mirror`). It never imports the facade.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import mcp_core
from kiro_crew.validation import SESSION_HANDOFF_SCHEMA, validate_tool_args


def session_handoff(name: str, args: dict[str, Any]) -> str:
    """Start a trackable Kiro Crew session for work handed over from outside.

    Stateless, and it must be: the caller is an EXTERNAL agent, so it has no
    Kiro Crew session identity at all (no gateway-injected session key, no signed
    host pid). Resolving one would either fail closed or — with the lenient
    resolver — walk process ancestors and attach to whatever slot happens to be
    up the tree. Creating a NEW session is what makes identity unnecessary here:
    there is nothing to target and nothing to spoof, and the session that appears
    belongs to the operator rather than to the caller.

    Every decision lives behind ``POST /api/chat/handoff`` in
    ``dashboard.handoff`` — the operator opt-in, the closed origin set, the
    project vetting, the unattended wiring, the audit — so this tool and the HTTP
    route cannot drift apart.
    """
    args = validate_tool_args(args, SESSION_HANDOFF_SCHEMA)
    d = mcp_core._post(
        "/api/chat/handoff",
        {
            "origin": args["origin"],
            "prompt": args["prompt"],
            "project": args.get("project", ""),
            "title": args.get("title", ""),
            "agent": args.get("agent", ""),
            "model": args.get("model", ""),
            "start": args.get("start", True),
        },
    )
    if d.get("error"):
        # The refusal text is operator-facing and already actionable (it names
        # the setting to turn on), so it is surfaced verbatim rather than
        # flattened into a generic failure the caller cannot act on.
        return f"Error: {d['error']}"
    slot = str(d.get("slot", ""))
    if d.get("started"):
        state = "running now"
    elif d.get("queued"):
        state = "queued behind the session's current turn"
    else:
        state = "created and parked (nothing sent)"
    where = f" in {d['project']}" if d.get("project") else ""
    return (
        f"Handed over to Kiro Crew session {slot}{where} — {state}. "
        "It runs under the approval grants already in place and will stop at any "
        "tool they do not cover; tell the user to open that session in Kiro Crew "
        "to watch or continue it."
    )
