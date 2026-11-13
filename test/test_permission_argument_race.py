"""The verdict a deferred shell permission request still has to earn.

Some adapters announce a tool call, ask permission, and only then finish
streaming the arguments. ``_defer_opencode_shell_permission`` gates such a
request once its streamed arguments land instead of gating the announcement —
what is pinned here is that deferral changes only WHEN the verdict is
computed, never the verdict itself: a request gated on real bytes is judged on
those bytes, and a request that reaches the consumer unverified still draws
the existing deny-by-default refusal.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

CMD = "ssh host 'python3 sync.py --dry-run'"
TOOL_ID = "call_xd9OgN52vOYe46tWH4o9o48J"


def test_the_gate_denies_an_unverified_request_and_allows_the_verified_one():
    """End to end over the real gate, not a stand-in.

    Both halves matter: the deny that deferral postpones must still be a deny,
    and a command-bearing request must reach the gate with bytes it can
    actually read.
    """
    from kiro_crew.hooks import TOOL_DENY, HookManager

    manager = HookManager({})

    unverified = manager.on_tool_call(
        "a description the LLM wrote", command=None, is_shell=True, tool_kind="execute"
    )
    assert unverified.action == TOOL_DENY
    assert "could not be verified" in (unverified.reason or "")

    verified = manager.on_tool_call(
        "a description the LLM wrote", command=CMD, is_shell=True, tool_kind="execute"
    )
    assert verified.action != TOOL_DENY, "an ordinary ssh dry-run must not be denied"


def test_deferral_cannot_rescue_a_command_the_rules_refuse():
    """Deferring the read must not launder a genuinely denied command."""
    from kiro_crew.hooks import TOOL_DENY, HookManager

    manager = HookManager({})
    verdict = manager.on_tool_call(
        "tidy up", command="rm -rf ~", is_shell=True, tool_kind="execute"
    )
    assert verdict.action == TOOL_DENY


def test_the_recorded_opencode_payload_yields_a_readable_command():
    """The shape measured on the live backend, as persisted by the session store."""
    tool_input = json.dumps(
        {"command": CMD, "workdir": "/Users/x/My Apps/vps-fleet", "timeout": 120000}
    )
    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        tool_call_id=TOOL_ID,
        is_shell=True,
        tool_input=tool_input,
    )
    assert event.shell_command == CMD


@pytest.mark.parametrize("params", [{"cmd": CMD}, {"input": {"command": CMD}}, {}])
def test_an_unrecognized_argument_shape_still_reads_as_unverifiable(params):
    """Deferral is not a licence to guess at a shape nobody recognizes."""
    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        tool_call_id=TOOL_ID,
        is_shell=True,
        raw_tool_params=params,
    )
    assert event.shell_command is None
