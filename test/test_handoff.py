"""External-agent handoff: what it opens, and what it deliberately does not.

The feature lets Claude Code / OpenClaw start a trackable Kiro Crew session
(upstream kirodotdev/KiroCrew#7697, unimplemented). Because the open position
authorises an autonomous run on the host, most of what is pinned here is the
CLOSED direction: the default refuses, an unreadable ceiling refuses, an unknown
origin refuses, and a permitted handoff gains no approval authority it did not
already have.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew import workflow_policy
from kiro_crew.dashboard import handoff
from kiro_crew.validation import SESSION_HANDOFF_SCHEMA, validate_tool_args


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    path = tmp_path / "workflow_policy.json"
    monkeypatch.setattr(workflow_policy, "workflow_policy_path", lambda: path)
    return path


def _permit(path) -> None:
    path.write_text(json.dumps({"allow_external_handoff": True}), encoding="utf-8")


class _Slot:
    """The slice of ``_ChatSlot`` the chokepoint touches."""

    def __init__(self) -> None:
        self.key = "chat-99-1788800000"
        self.project = ""
        self.running = False
        self.appended: list[tuple[str, str]] = []
        self.queued: list[str] = []

    def append(self, role: str, text: str, _cls: str = "") -> None:
        self.appended.append((role, text))

    def queue_append(self, text: str, meta=None) -> None:
        self.queued.append(text)


class _State:
    def __init__(self, slot: _Slot) -> None:
        self.slot = slot
        self.created_kwargs: dict = {}

    def get_or_create_slot(self, **kwargs):
        self.created_kwargs = kwargs
        return self.slot

    def run_background_turn(self, slot, coro):
        """Sync passthrough, deliberately.

        The real method is ``async`` and awaits the coroutine (under the
        unattended semaphore for an app-owned slot). Returning it UNCHANGED here
        hands the inner ``_run_chat`` coroutine straight to the spawn double,
        which closes it -- otherwise the inner coroutine is never awaited and
        every test in this file emits a RuntimeWarning that would mask a real
        one later. What these tests assert is that the spawn happened, not what
        the turn did.
        """
        return coro


@pytest.fixture
def wired(monkeypatch):
    """Intercept the turn launch so no real agent spawn happens."""
    launched: list[str] = []

    def _spawn(state, slot, coro, **kwargs):
        coro.close()  # never awaited here; closing avoids a pending-coroutine warning
        launched.append("spawned")
        return SimpleNamespace(cancel=lambda: None)

    async def _run_chat(state, slot, message, **kwargs):  # pragma: no cover - never awaited
        return None

    import kiro_crew.dashboard.chat_runner as chat_runner
    import kiro_crew.dashboard.turn_dispatch as turn_dispatch

    monkeypatch.setattr(turn_dispatch, "spawn_guarded_turn", _spawn, raising=True)
    monkeypatch.setattr(chat_runner, "_run_chat", _run_chat, raising=True)
    return launched


# ── the opt-in ──


def test_the_default_refuses(keystone, wired):
    """Installing the MCP server must not by itself authorise autonomous runs."""
    slot = _Slot()
    with pytest.raises(handoff.HandoffRefused) as exc:
        asyncio.run(handoff.start_handoff(_State(slot), origin="claude_code", prompt="do it"))
    assert exc.value.code == "handoff_not_permitted"
    assert exc.value.status == 403
    assert wired == [], "nothing may run before the operator opts in"


def test_a_permitted_handoff_starts_a_turn(keystone, wired):
    _permit(keystone)
    slot = _Slot()
    state = _State(slot)
    result = asyncio.run(
        handoff.start_handoff(state, origin="claude_code", prompt="finish the migration")
    )
    assert result["started"] is True
    assert result["queued"] is False
    assert result["slot"] == slot.key
    assert wired == ["spawned"]


def test_an_unreadable_ceiling_refuses(keystone, wired, monkeypatch):
    """Fails CLOSED, including when the read raises rather than returning False."""

    def _boom():
        raise OSError("disk gone")

    _permit(keystone)
    monkeypatch.setattr(workflow_policy, "allow_external_handoff", _boom)
    with pytest.raises(handoff.HandoffRefused) as exc:
        asyncio.run(handoff.start_handoff(_State(_Slot()), origin="openclaw", prompt="x"))
    assert exc.value.code == "handoff_not_permitted"
    assert wired == []


@pytest.mark.parametrize("value", ["true", 1, "True", None, [], {}])
def test_only_a_real_json_true_permits(keystone, wired, value):
    keystone.write_text(json.dumps({"allow_external_handoff": value}), encoding="utf-8")
    with pytest.raises(handoff.HandoffRefused):
        asyncio.run(handoff.start_handoff(_State(_Slot()), origin="codex", prompt="x"))


# ── the closed origin set ──


@pytest.mark.parametrize("origin", ["claude_code", "openclaw", "codex", "external"])
def test_every_known_origin_is_accepted(keystone, wired, origin):
    _permit(keystone)
    result = asyncio.run(handoff.start_handoff(_State(_Slot()), origin=origin, prompt="x"))
    assert result["origin"] == origin


@pytest.mark.parametrize("origin", ["", "CLAUDE_CODE", "claude code", "evil", None, 7])
def test_an_unknown_origin_refuses_before_the_ceiling_is_read(keystone, wired, origin):
    """A caller's own mistake must not read as the operator's configuration.

    The origin lands on the slot, in the sidebar, and in the provenance line the
    model reads, so it is validated first and by exact membership.
    """
    _permit(keystone)
    with pytest.raises(handoff.HandoffRefused) as exc:
        asyncio.run(handoff.start_handoff(_State(_Slot()), origin=origin, prompt="x"))
    assert exc.value.code == "unknown_origin"
    assert exc.value.status == 400


def test_the_tool_schema_and_the_chokepoint_agree_on_the_origin_set():
    """A value the schema accepts and the chokepoint refuses is a 400 nobody can act on."""
    field = next(f for f in SESSION_HANDOFF_SCHEMA.fields if f.name == "origin")
    assert field.allowed == handoff.KNOWN_ORIGINS


# ── the prompt ──


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t ", None, 5])
def test_an_empty_prompt_refuses(keystone, wired, prompt):
    _permit(keystone)
    with pytest.raises(handoff.HandoffRefused) as exc:
        asyncio.run(handoff.start_handoff(_State(_Slot()), origin="external", prompt=prompt))
    assert exc.value.code == "empty_prompt"


def test_an_oversized_prompt_refuses(keystone, wired):
    """A handoff is a work item, not a transcript transfer."""
    _permit(keystone)
    with pytest.raises(handoff.HandoffRefused) as exc:
        asyncio.run(
            handoff.start_handoff(
                _State(_Slot()), origin="external", prompt="x" * (handoff.MAX_PROMPT_CHARS + 1)
            )
        )
    assert exc.value.code == "prompt_too_long"


def test_the_prompt_carries_its_provenance(keystone, wired):
    """The model must not read a handoff as the operator typing.

    It arrived from automation and the user may not be present, which is exactly
    what the injected-messages rule is about.
    """
    _permit(keystone)
    slot = _Slot()
    asyncio.run(handoff.start_handoff(_State(slot), origin="openclaw", prompt="run the dry-run"))
    role, text = slot.appended[-1]
    assert role == "user"
    assert text.startswith("[handed over by openclaw")
    assert "the user may not be present" in text
    assert text.rstrip().endswith("run the dry-run")


# ── unattended wiring ──


def test_the_slot_is_app_owned_so_it_lands_on_the_unattended_path(keystone, wired):
    """``_app`` is what makes the deny-fast window and the background cap apply.

    A handed-over session has nobody watching at the moment it starts, so it must
    not sit on the interactive approval path holding a slot.
    """
    _permit(keystone)
    state = _State(_Slot())
    asyncio.run(handoff.start_handoff(state, origin="claude_code", prompt="x"))
    assert state.created_kwargs["app"] == "handoff:claude_code"
    assert state.created_kwargs["origin"] == "app"


def test_a_busy_slot_queues_instead_of_running(keystone, wired):
    """ "It is running" and "it will run later" must not look the same to a poller."""
    _permit(keystone)
    slot = _Slot()
    slot.running = True
    result = asyncio.run(handoff.start_handoff(_State(slot), origin="external", prompt="x"))
    assert result["queued"] is True
    assert result["started"] is False
    assert wired == [], "no second turn may be spawned onto a running slot"
    assert len(slot.queued) == 1


def test_start_false_creates_the_session_without_sending(keystone, wired):
    _permit(keystone)
    slot = _Slot()
    result = asyncio.run(
        handoff.start_handoff(_State(slot), origin="external", prompt="later", start=False)
    )
    assert result == {
        "slot": slot.key,
        "origin": "external",
        "started": False,
        "queued": False,
        "project": "",
    }
    assert slot.appended == []
    assert wired == []


# ── the project directory ──


def test_a_project_is_resolved_and_kept(keystone, wired, tmp_path):
    _permit(keystone)
    slot = _Slot()
    result = asyncio.run(
        handoff.start_handoff(_State(slot), origin="external", prompt="x", project=str(tmp_path))
    )
    assert result["project"] == str(tmp_path.resolve())
    assert slot.project == str(tmp_path.resolve())


def test_a_missing_project_directory_refuses(keystone, wired, tmp_path):
    _permit(keystone)
    with pytest.raises(handoff.HandoffRefused) as exc:
        asyncio.run(
            handoff.start_handoff(
                _State(_Slot()), origin="external", prompt="x", project=str(tmp_path / "nope")
            )
        )
    assert exc.value.code == "project_not_a_directory"


def test_a_sensitive_project_directory_refuses(keystone, wired):
    """A handoff must not reach a directory the dashboard would refuse to open."""
    _permit(keystone)
    with pytest.raises(handoff.HandoffRefused) as exc:
        asyncio.run(
            handoff.start_handoff(_State(_Slot()), origin="external", prompt="x", project="~/.ssh")
        )
    assert exc.value.code == "project_denied"
    assert exc.value.status == 403


# ── the sidebar label ──


@pytest.mark.parametrize(
    "app,expected",
    [
        ("handoff:claude_code", "claude_code"),
        ("handoff:openclaw", "openclaw"),
        ("handoff:evil", ""),
        ("handoff:", ""),
        ("mochi", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_the_origin_label_is_validated_on_the_way_out(app, expected):
    """A hand-edited session file must not put free text on the sidebar."""
    assert handoff.origin_of(app) == expected


def test_the_serializer_never_raises_on_a_slot_without_the_tag():
    from kiro_crew.dashboard.state import DashboardState

    assert DashboardState._handoff_origin(None, SimpleNamespace()) == ""


# ── what the tool advertises ──


def test_the_tool_is_registered_with_a_handler():
    from kiro_crew.mcp_tools import control

    assert "session_handoff" in control.HANDLERS
    assert any(t["name"] == "session_handoff" for t in control.schemas())


def test_the_tool_description_states_what_it_does_not_grant():
    """The model will relay this to the user, so the limit has to be in it.

    A caller that promises an unattended run will finish is the failure this
    wording exists to prevent.
    """
    from kiro_crew.mcp_tools import control

    desc = next(t for t in control.schemas() if t["name"] == "session_handoff")["description"]
    assert "NO approval authority" in desc
    assert "OFF by default" in desc


def test_the_tool_defaults_to_starting_the_turn():
    args = validate_tool_args({"origin": "claude_code", "prompt": "x"}, SESSION_HANDOFF_SCHEMA)
    assert args["start"] is True
