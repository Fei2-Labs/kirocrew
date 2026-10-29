"""A spawn tool's brief is prose for a sub-agent, not a shell command line.

The always-enforced tool_input scan in ``_resolve_permission`` runs on every
surface, including the unattended ones with no hook gate (cron, Slack,
workflow, heartbeat). It handed every string of a ``spawn_run`` call to the
command-text rules, so a brief that merely DESCRIBED a destructive git step,
or a copy to this host over ssh, was refused as if the parent were running it,
and the cron saw only a bare refusal. The spawn tools' brief fields are now
listed in ``platform.tool_paths.MCP_DOCUMENT_BODY_FIELDS``, keyed by the
trusted core-server identity: the brief skips the command-text rules and keeps
the path tier and the size ceiling, every other field keeps the full scan, and
an unknown tool or identity keeps the full scan. Whatever the sub-agent then
runs is its own tool call and meets the deny rules there.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.hooks import TOOL_DENY, HookManager
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission, stream_and_collect
from kiro_crew.platform.tool_paths import mcp_document_body_keys, split_document_bodies
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    LLMEvent,
)
from kiro_crew.slack.gateway import _GateTally

# Built by concatenation so this file never carries the command line itself.
_DENIED_STEP = "git " + "reset " + "--hard" + " origin/main"
_BRIEF = (
    "Rebase the feature branch onto main. If the tree is in a bad state, the "
    f"usual recovery is `{_DENIED_STEP}`, but ask first.\n"
)
_CORE = "kirocrew-core"


class _RecordingProvider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _event(
    params: dict,
    *,
    tool_name: str = "spawn_run",
    server: str = _CORE,
    is_shell: bool = False,
    trusted: bool = True,
    identity: bool = True,
    title: str | None = None,
) -> LLMEvent:
    """A permission event as the ACP client emits it for an MCP tool call."""
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title if title is not None else f"@{server}/{tool_name}",
        request_id="r1",
        tool_kind="other",
        tool_input=json.dumps(params, indent=2),
        raw_tool_params=params,
        raw_params_trusted=trusted,
        is_shell=is_shell,
        mcp_identity_trusted=identity,
        mcp_server_name=server,
        tool_name=tool_name,
    )


async def _resolve(event: LLMEvent) -> tuple[bool, list[dict]]:
    provider = _RecordingProvider()
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        approved = await _resolve_permission(
            provider,  # type: ignore[arg-type]
            event,
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
        )
    return approved, rows


def _error(rows: list[dict]) -> str:
    assert len(rows) == 1, rows
    return str(rows[0].get("error") or "")


class TestTheBriefFieldsAreListed:
    @pytest.mark.parametrize(
        ("tool", "fields"),
        [
            ("spawn_run", {"task", "tasks[]", "tasks[].task"}),
            ("spawn_continue", {"task"}),
            ("spawn_steer", {"message"}),
            ("spawn_sub_agents", {"agents[].prompt"}),
        ],
    )
    def test_core_spawn_tools_name_their_brief(self, tool: str, fields: set[str]) -> None:
        assert mcp_document_body_keys(tool, _CORE) == frozenset(fields)
        assert mcp_document_body_keys(f"mcp__{_CORE}__{tool}", _CORE) == frozenset(fields)

    @pytest.mark.parametrize("server", ["", "other-server"])
    def test_another_server_has_no_brief(self, server: str) -> None:
        assert mcp_document_body_keys("spawn_run", server) == frozenset()


class TestSplittingNestedFields:
    def test_every_listed_shape_is_split_and_the_rest_is_kept(self) -> None:
        params = {
            "task": "a",
            "tasks": ["b", {"task": "c", "label": "L"}, 7],
            "cwd": "/w",
        }
        rest, bodies = split_document_bodies(params, frozenset({"task", "tasks[]", "tasks[].task"}))
        assert sorted(bodies) == ["a", "b", "c"]
        assert rest == {"tasks": [{"label": "L"}, 7], "cwd": "/w"}
        # The caller's params are not modified.
        assert params["tasks"][1] == {"task": "c", "label": "L"}

    def test_a_non_string_at_a_body_path_stays_scanned(self) -> None:
        params = {"task": {"cmd": "x"}, "agents": "not-a-list"}
        rest, bodies = split_document_bodies(params, frozenset({"task", "agents[].prompt"}))
        assert bodies == []
        assert rest == params

    def test_the_top_level_form_is_unchanged(self) -> None:
        rest, bodies = split_document_bodies({"content": "x", "title": "t"}, frozenset({"content"}))
        assert (rest, bodies) == ({"title": "t"}, ["x"])


class TestTheBriefIsNotACommandLine:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "params"),
        [
            ("spawn_run", {"task": _BRIEF}),
            ("spawn_run", {"tasks": [_BRIEF, {"task": _BRIEF, "label": "fix"}]}),
            ("spawn_continue", {"conversation": "c-1", "task": _BRIEF}),
            ("spawn_steer", {"agent_id": "a-1", "message": _BRIEF}),
            ("spawn_sub_agents", {"agents": [{"agent_or_mode": "dev", "prompt": _BRIEF}]}),
        ],
    )
    async def test_a_brief_describing_a_denied_step_is_allowed(
        self, tool: str, params: dict
    ) -> None:
        approved, rows = await _resolve(_event(params, tool_name=tool))
        assert approved is True, _error(rows)

    @pytest.mark.asyncio
    async def test_the_full_scan_still_refuses_the_same_brief_and_names_the_rule(self) -> None:
        # Untrusted identity: the exemption does not apply, the rule still fires,
        # and the audit row's reason names the pattern that matched.
        approved, rows = await _resolve(_event({"task": _BRIEF}, identity=False))
        assert approved is False
        assert "Blocked by security policy: git reset --hard" in _error(rows)


class TestEverythingElseKeepsTheFullScan:
    @pytest.mark.asyncio
    async def test_the_same_step_as_a_shell_command_is_denied(self) -> None:
        ev = _event(
            {"command": _DENIED_STEP},
            tool_name="execute_bash",
            server="",
            is_shell=True,
            title=_DENIED_STEP,
        )
        approved, rows = await _resolve(ev)
        assert approved is False
        assert "git reset --hard" in _error(rows)

    @pytest.mark.asyncio
    async def test_a_shell_frame_naming_the_spawn_tool_is_denied(self) -> None:
        approved, _rows = await _resolve(_event({"task": _BRIEF}, is_shell=True))
        assert approved is False

    @pytest.mark.asyncio
    async def test_a_same_named_tool_on_another_server_is_denied(self) -> None:
        approved, _rows = await _resolve(_event({"task": _BRIEF}, server="other-server"))
        assert approved is False

    @pytest.mark.asyncio
    async def test_an_unlisted_core_tool_is_denied(self) -> None:
        approved, _rows = await _resolve(_event({"task": _BRIEF}, tool_name="cron_add"))
        assert approved is False

    @pytest.mark.asyncio
    async def test_untrusted_params_are_denied(self) -> None:
        approved, _rows = await _resolve(_event({"task": _BRIEF}, trusted=False))
        assert approved is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "params",
        [
            {"task": "ok", "label": _DENIED_STEP},
            {"task": "ok", "cwd": _DENIED_STEP},
            {"tasks": [{"task": "ok", "model": _DENIED_STEP}]},
            {"agents": [{"prompt": "ok", "agent_or_mode": _DENIED_STEP}]},
        ],
    )
    async def test_a_non_brief_field_is_still_command_scanned(self, params: dict) -> None:
        tool = "spawn_sub_agents" if "agents" in params else "spawn_run"
        approved, _rows = await _resolve(_event(params, tool_name=tool))
        assert approved is False

    @pytest.mark.asyncio
    async def test_a_credential_path_as_the_brief_is_denied(self) -> None:
        approved, rows = await _resolve(_event({"task": "~/.aws/credentials"}))
        assert approved is False
        assert "sensitive path" in _error(rows)


class TestTheChildStillMeetsTheRule:
    def test_the_childs_shell_call_running_the_step_is_denied(self) -> None:
        # A sub-agent that acts on the brief issues its own shell call; the
        # child's permission ladder consults this same hook gate, whose deny
        # tier judges the recovered command.
        verdict = HookManager().on_tool_call(
            f"Running: {_DENIED_STEP}", command=_DENIED_STEP, is_shell=True
        )
        assert verdict.action == TOOL_DENY


class TestTheRefusalNamesTheRule:
    @pytest.mark.asyncio
    async def test_the_gate_callback_receives_the_matched_rule(self) -> None:
        class _Provider(_RecordingProvider):
            async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
                yield _event({"task": _BRIEF}, identity=False)
                yield LLMEvent(kind=EVENT_COMPLETE, text="")

        seen: list[tuple[str, bool, bool, str]] = []
        await stream_and_collect(
            _Provider(),  # type: ignore[arg-type]
            "q",
            approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
            retry_transient=False,
            on_tool_gate=lambda t, a, b, r: seen.append((t, a, b, r)),
        )
        assert len(seen) == 1
        _title, approved, blocked, reason = seen[0]
        assert (approved, blocked) == (False, True)
        assert "Blocked by security policy: git reset --hard" in reason

    def test_the_cron_summary_names_the_rule(self) -> None:
        tally = _GateTally()
        tally.note(
            "@kirocrew-core/spawn_run",
            False,
            True,
            "Blocked by security policy: git reset --hard.*\noperator note",
        )
        summary = tally.refusal_summary()
        assert "spawn_run (Blocked by security policy: git reset --hard.*)" in summary
        assert "operator note" not in summary

    def test_a_refusal_without_a_reason_reads_as_before(self) -> None:
        tally = _GateTally()
        tally.note("@kirocrew-core/spawn_run", False, True)
        assert tally.refusal_summary().endswith("spawn_run")
