"""Client-replacement forwarding and ``_to_llm_event`` provenance-flag tests.

Split out of ``test_acp_provider.py`` for the file-line cap; fixtures/helpers
are imported from it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import ACP_BACKEND_GOOSE
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from test_acp_provider import TestStartKiroRuntimeModelEntitlement as _ModelEntitlement
from test_acp_provider import _build_provider


def test_child_fidelity_aware_survives_client_replacement():
    """The dashboard sets the fidelity opt-in on the OUTER AcpProvider before
    startup; for the kiro backend the inner client is later REPLACED with an
    AcpSessionProvider (_start_kiro_runtime_impl). The flag must be a real
    forwarding property — a plain setattr would be inert and the handle
    would fail-close the dashboard's child permission requests instead of
    showing the interactive card."""
    provider = _build_provider("")  # "" = kiro, the runtime-backed default

    provider.child_fidelity_aware = True
    assert provider.child_fidelity_aware is True
    # Forwarded to the current inner client.
    assert provider._client.child_fidelity_aware is True
    # Stored on the provider itself, independent of the (soon-discarded)
    # placeholder client — this is what _start_kiro_runtime_impl re-applies
    # to the real AcpSessionProvider at replacement time.
    provider._client = MagicMock()
    assert provider.child_fidelity_aware is True


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [True, False])
async def test_the_keepalive_routes_mark_survives_client_replacement(marked):
    """The registry ``/api/session-keepalive`` reads marks the OUTER provider when it
    publishes it, which can be before startup REPLACES the placeholder client. The
    start marks the real ``AcpSessionProvider`` then, so its handle times calls on
    Kiro Crew's own servers; an unmarked provider (``kirocrew chat``'s) starts one
    that does not."""
    provider = _ModelEntitlement()._kiro_provider("auto")
    placeholder = provider._client
    if marked:
        provider.mark_keepalive_routed()
        placeholder.mark_keepalive_routed.assert_called_once_with()
    handle = MagicMock()
    handle.session_id = "kiro-sess-1"
    handle.model_pin_refused = ""
    handle.available_models = []
    runtime = MagicMock(
        pid=UNALLOCATABLE_PID, spawn=AsyncMock(), create_session=AsyncMock(return_value=handle)
    )
    started: list[MagicMock] = []

    def session_provider(h, r, **_kw):
        started.append(MagicMock(_handle=h, _runtime=r, resumed=False))
        return started[-1]

    with (
        patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
        patch("kiro_crew.providers.acp.AcpSessionProvider", side_effect=session_provider),
        patch("pathlib.Path.exists", return_value=False),
    ):
        await provider._start_kiro_runtime()

    assert provider._client is started[0]
    assert started[0].mark_keepalive_routed.called is marked


def test_to_llm_event_preserves_provenance_flags():
    """`AcpProvider._to_llm_event` reconstructs the event — dropping a
    provenance field would zero it to False: for the params pair that flips
    child_low_fidelity to True for EVERY child permission event on this
    surface (making the full-fidelity half of the feature inert); for
    mcp_identity_trusted it revokes the verified-identity half
    (child_mcp_identity_trusted) the unconditional grant paths rely on."""
    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent
    from kiro_crew.providers.acp import AcpProvider

    src = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=9,
        title="Running: sha256sum x",
        sub_session_id="child-a",
        raw_tool_params={"command": "sha256sum x"},
        raw_params_trusted=True,
        is_shell=True,
        shell_classified=True,
    )
    assert src.child_low_fidelity is False
    out = AcpProvider._to_llm_event(src)
    assert out.raw_params_trusted is True
    assert out.shell_classified is True
    assert out.child_low_fidelity is False


def test_to_llm_event_preserves_mcp_identity_trusted():
    """The identity-provenance flag crosses the provider copy: dropping it
    from the copy list silently reads False and revokes
    child_mcp_identity_trusted for every crossing event (the drop-to-False
    trap the explicit flag was added to guard against — it must fail closed
    only for genuinely untrusted population, never for a copy)."""
    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent
    from kiro_crew.providers.acp import AcpProvider

    src = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=11,
        title="@example-server/get-item",
        sub_session_id="child-a",
        shell_classified=True,
        is_shell=False,
        mcp_server_name="example-server",
        tool_name="get-item",
        tool_identity_trusted=True,
        mcp_identity_trusted=True,
    )
    assert src.child_mcp_identity_trusted is True
    out = AcpProvider._to_llm_event(src)
    assert out.tool_identity_trusted is True
    assert out.mcp_identity_trusted is True
    assert out.child_mcp_identity_trusted is True
    assert out.child_unconditional_grant_eligible is True
    # And the flag is copied, not invented: an untrusted source stays False.
    src_untrusted = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=12,
        sub_session_id="child-a",
        shell_classified=True,
        is_shell=False,
        mcp_server_name="example-server",
        tool_name="get-item",
    )
    out_untrusted = AcpProvider._to_llm_event(src_untrusted)
    assert out_untrusted.tool_identity_trusted is False
    assert out_untrusted.mcp_identity_trusted is False
    assert out_untrusted.child_mcp_identity_trusted is False


class TestBackendModelWireIds:
    def test_generic_spec_id_passes_through(self):
        from kiro_crew.dashboard.chat_handlers import _wire_model_id

        provider = _build_provider(ACP_BACKEND_GOOSE)
        assert _wire_model_id(provider, "provider/model") == "provider/model"
