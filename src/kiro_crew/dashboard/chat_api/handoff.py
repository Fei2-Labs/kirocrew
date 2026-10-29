"""Handoff intake: ``POST /api/chat/handoff`` — an external agent starts a session.

Fork-owned. Thin by design: every decision (the operator opt-in, the closed
origin set, the project vetting, the unattended wiring, the audit) lives in
:mod:`kiro_crew.dashboard.handoff`, because the ``session_handoff`` MCP tool
reaches the same chokepoint and the two must not drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import read_bounded_json

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import DashboardState


async def api_chat_handoff(request: web.Request) -> web.Response:
    """POST /api/chat/handoff — an external agent starts a trackable session.

    Thin: every decision (the operator opt-in, the closed origin set, the
    project vetting, the unattended wiring, the audit) lives in
    ``dashboard.handoff``, because the ``session_handoff`` MCP tool reaches the
    same chokepoint and the two must not drift.

    Returns 403 with ``code: "handoff_not_permitted"`` while the keystone opt-in
    is off, which is the default — installing the MCP server does not by itself
    authorise autonomous runs on this host.
    """
    from kiro_crew.dashboard.handoff import HandoffRefused, start_handoff

    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    try:
        result = await start_handoff(
            state,
            origin=body.get("origin", ""),
            prompt=body.get("prompt", ""),
            project=body.get("project", "") or "",
            agent=body.get("agent", "") or "",
            model=body.get("model", "") or "",
            title=body.get("title", "") or "",
            start=body.get("start", True) is not False,
        )
    except HandoffRefused as exc:
        if exc.status == 400:
            return web.json_response({"error": str(exc), "code": exc.code}, status=400)
        return web.json_response({"error": str(exc), "code": exc.code}, status=403)
    return web.json_response(result)
