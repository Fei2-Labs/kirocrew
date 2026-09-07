"""A shell permission request that arrives before its arguments.

Some adapters announce a tool call, ask permission, and only then finish
streaming the arguments. Gating on the announcement reads no command, so
``HookManager.on_tool_call``'s deny-by-default backstop refuses an ordinary
call — measured on the opencode backend as three SEL events one millisecond
apart (``denied``, ``refined``, ``refined``), with the recorded tool input
carrying the full ``{"command": …}`` all along.

What is pinned here is that parking changes only WHEN the verdict is computed,
never the verdict itself: a request whose arguments arrive is gated on real
bytes, and a request whose arguments never arrive still reaches the consumer
unverified, so the existing deny stands. The turn may never end holding one,
because the agent is blocked on the response.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.acp.client import _MAX_PARKED_PERMISSIONS, AcpClient
from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, EVENT_TOOL_CALL, AcpEvent

CMD = "ssh host 'python3 sync.py --dry-run'"
TOOL_ID = "call_xd9OgN52vOYe46tWH4o9o48J"


def _client() -> AcpClient:
    """A client with only the state these helpers touch.

    ``__new__`` deliberately, not a constructed client: the helpers are pure
    bookkeeping over the provenance caches, and a real spawn would need a
    backend process. The one attribute the park path reads defensively
    (``_parked_permissions``) is set here, and its absence is covered by
    :func:`test_a_client_without_the_park_state_denies_as_before`.
    """
    c = AcpClient.__new__(AcpClient)
    c._parked_permissions = {}
    c._permission_options = {}
    return c


def _perm_event(*, is_shell: bool, command: str | None, tool_call_id: str = TOOL_ID) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="a description the LLM wrote",
        tool_call_id=tool_call_id,
        request_id="11",
        is_shell=is_shell,
        raw_tool_params={"command": command} if command else None,
    )


# ── what is parkable ──


def test_a_shell_request_without_its_arguments_is_parked():
    c = _client()
    event = _perm_event(is_shell=True, command=None)
    assert event.shell_command is None, "fixture must reproduce the unverifiable state"
    assert c._park_unverified_permission(event, SimpleNamespace(params={})) is True
    assert TOOL_ID in c._parked_permissions


def test_a_request_whose_command_is_already_readable_is_gated_now():
    """The common case must not be delayed by one frame."""
    c = _client()
    event = _perm_event(is_shell=True, command=CMD)
    assert event.shell_command == CMD
    assert c._park_unverified_permission(event, SimpleNamespace(params={})) is False
    assert c._parked_permissions == {}


def test_a_non_shell_request_is_never_parked():
    """Parking exists for the shell gate; nothing else waits on a command."""
    c = _client()
    assert (
        c._park_unverified_permission(
            _perm_event(is_shell=False, command=None), SimpleNamespace(params={})
        )
        is False
    )
    assert c._parked_permissions == {}


def test_a_request_with_no_tool_call_id_is_not_parked():
    """There would be nothing to match a refinement against."""
    c = _client()
    assert (
        c._park_unverified_permission(
            _perm_event(is_shell=True, command=None, tool_call_id=""), SimpleNamespace(params={})
        )
        is False
    )
    assert c._parked_permissions == {}


def test_a_non_permission_event_is_not_parked():
    c = _client()
    event = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=TOOL_ID, is_shell=True)
    assert c._park_unverified_permission(event, SimpleNamespace(params={})) is False


def test_the_cap_stops_the_map_growing_and_falls_back_to_the_old_path():
    """A backend that never refines must not accumulate parked requests.

    Past the cap the request is gated WITHOUT its arguments — i.e. exactly the
    behaviour before this mechanism existed — rather than parked forever.
    """
    c = _client()
    for i in range(_MAX_PARKED_PERMISSIONS):
        assert (
            c._park_unverified_permission(
                _perm_event(is_shell=True, command=None, tool_call_id=f"call_{i}"),
                SimpleNamespace(params={}),
            )
            is True
        )
    assert len(c._parked_permissions) == _MAX_PARKED_PERMISSIONS
    assert (
        c._park_unverified_permission(
            _perm_event(is_shell=True, command=None, tool_call_id="one-too-many"),
            SimpleNamespace(params={}),
        )
        is False
    )
    assert len(c._parked_permissions) == _MAX_PARKED_PERMISSIONS


def test_a_client_without_the_park_state_denies_as_before():
    """A ``__new__``-constructed client that never ran __init__ has nowhere to park.

    The codebase constructs such clients (embedders, legacy paths), and the
    fallback must be the unchanged deny rather than invented state.
    """
    c = AcpClient.__new__(AcpClient)
    assert not hasattr(c, "_parked_permissions")
    assert (
        c._park_unverified_permission(
            _perm_event(is_shell=True, command=None), SimpleNamespace(params={})
        )
        is False
    )


# ── release ──


def _rebuild_with_command(monkeypatch, client: AcpClient, command: str | None) -> None:
    """Stand in for the parser reading the now-populated provenance caches."""
    monkeypatch.setattr(
        AcpClient,
        "_build_permission_event",
        lambda self, msg: _perm_event(is_shell=True, command=command),
        raising=True,
    )


def test_the_refinement_releases_the_request_with_real_bytes(monkeypatch):
    c = _client()
    c._parked_permissions[TOOL_ID] = SimpleNamespace(params={})
    _rebuild_with_command(monkeypatch, c, CMD)

    released = c._release_parked_permission(TOOL_ID)
    assert released is not None
    assert released.shell_command == CMD, "the gate must see the adapter's own bytes"
    assert c._parked_permissions == {}


def test_a_refinement_that_still_carries_no_command_keeps_the_request_parked(monkeypatch):
    """A partial refinement must not consume the park.

    Releasing it here would gate an unverified request early and lose the chance
    that a later refinement completes it; the flush is what ends the wait.
    """
    c = _client()
    c._parked_permissions[TOOL_ID] = SimpleNamespace(params={})
    _rebuild_with_command(monkeypatch, c, None)

    assert c._release_parked_permission(TOOL_ID) is None
    assert TOOL_ID in c._parked_permissions


def test_a_refinement_for_an_unrelated_tool_call_releases_nothing(monkeypatch):
    c = _client()
    c._parked_permissions[TOOL_ID] = SimpleNamespace(params={})
    _rebuild_with_command(monkeypatch, c, CMD)

    assert c._release_parked_permission("some-other-call") is None
    assert c._release_parked_permission("") is None
    assert TOOL_ID in c._parked_permissions


# ── flush: the turn may not end holding a request ──


def test_the_flush_emits_every_stranded_request(monkeypatch):
    """The agent blocks on the response, so a hang would be worse than a deny."""
    c = _client()
    for i in range(3):
        c._parked_permissions[f"call_{i}"] = SimpleNamespace(params={})
    _rebuild_with_command(monkeypatch, c, None)

    flushed = c._flush_parked_permissions()
    assert len(flushed) == 3
    assert c._parked_permissions == {}
    # Unverified on the way out: this is what keeps the existing deny-by-default
    # verdict for a request whose arguments never arrived.
    assert all(e.shell_command is None for e in flushed)


def test_the_flush_is_empty_and_cheap_when_nothing_is_parked():
    c = _client()
    assert c._flush_parked_permissions() == []
    c2 = AcpClient.__new__(AcpClient)
    assert c2._flush_parked_permissions() == []


def test_a_flush_rebuilds_rather_than_replaying_the_original(monkeypatch):
    """A refinement may land between the release check and the flush.

    Rebuilding means a late arrival is still honoured instead of denied on a
    stale reading of the same frame.
    """
    c = _client()
    c._parked_permissions[TOOL_ID] = SimpleNamespace(params={})
    _rebuild_with_command(monkeypatch, c, CMD)

    flushed = c._flush_parked_permissions()
    assert [e.shell_command for e in flushed] == [CMD]


# ── the verdict itself is unchanged ──


def test_the_gate_denies_an_unverified_request_and_allows_the_released_one():
    """End to end over the real gate, not a stand-in.

    Both halves matter: the deny that parking defers must still be a deny, and
    the released event must reach the gate with bytes it can actually read.
    """
    from kiro_crew.hooks import TOOL_DENY, HookManager

    manager = HookManager({})

    unverified = manager.on_tool_call(
        "a description the LLM wrote", command=None, is_shell=True, tool_kind="execute"
    )
    assert unverified.action == TOOL_DENY
    assert "could not be verified" in (unverified.reason or "")

    released = manager.on_tool_call(
        "a description the LLM wrote", command=CMD, is_shell=True, tool_kind="execute"
    )
    assert released.action != TOOL_DENY, "an ordinary ssh dry-run must not be denied"


def test_parking_cannot_rescue_a_command_the_rules_refuse():
    """Deferring the read must not launder a genuinely denied command."""
    from kiro_crew.hooks import TOOL_DENY, HookManager

    manager = HookManager({})
    verdict = manager.on_tool_call(
        "tidy up", command="rm -rf ~", is_shell=True, tool_kind="execute"
    )
    assert verdict.action == TOOL_DENY


def test_the_recorded_opencode_payload_yields_a_readable_command():
    """The shape measured on the live backend, as persisted by the session store."""
    from kiro_crew.acp.types import AcpEvent as Event

    tool_input = json.dumps(
        {"command": CMD, "workdir": "/Users/x/My Apps/vps-fleet", "timeout": 120000}
    )
    event = Event(
        kind=EVENT_PERMISSION_REQUEST,
        tool_call_id=TOOL_ID,
        is_shell=True,
        tool_input=tool_input,
    )
    assert event.shell_command == CMD


@pytest.mark.parametrize("params", [{"cmd": CMD}, {"input": {"command": CMD}}, {}])
def test_an_unrecognized_argument_shape_still_reads_as_unverifiable(params):
    """The park is not a licence to guess at a shape nobody recognizes."""
    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        tool_call_id=TOOL_ID,
        is_shell=True,
        raw_tool_params=params,
    )
    assert event.shell_command is None
