"""The spec-adapter ACP client: admission that runs before process creation.

Fork-owned. The first-class Kiro provider instantiates :class:`AcpClient`
directly (see :mod:`kiro_crew.acp.client_class`), so it gains no adapter
conditional, awaited step, or failure mode (harness-parity H13). The provider
selects :class:`SpecAdapterAcpClient` only for public-spec backends, and that
class adds the fork's two pre-spawn checks upstream's client does not run:

* the agent-profile admission (:mod:`kiro_crew.acp.spec_agent_guard`) — the
  session's agent spec must be permitted for this harness's dialect before any
  child exists; and
* the tool-gate floor (:func:`kiro_crew.acp.tool_gate.enforce`) — the session's
  work directory must not let the adapter's tool calls bypass the host
  permission gate.

Upstream's own session-permission arming (``_apply_session_permission_routing``,
gated on ``Routing.SESSION_CONFIG`` inside the shared handshake) still runs for
these backends exactly as for every other one; this module adds to it, it does
not replace it.
"""

from __future__ import annotations

import asyncio

from kiro_crew.acp import backends as acp_backends
from kiro_crew.acp import spec_agent_guard, tool_gate
from kiro_crew.acp.client_class import AcpClient
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable


async def _spec_adapter_initialize_session(self: AcpClient) -> None:
    """Complete the shared handshake, then arm adapter-only permission routing."""
    await AcpClient._initialize_session(self)
    # ensure_ready awaits this override before exposing the client to a prompt.
    await self._apply_session_permission_routing()


async def _spec_adapter_spawn(self: AcpClient) -> None:
    # The agent-profile and tool-routing checks need the session directory,
    # but no child may exist until both checks have passed.
    await asyncio.to_thread(self._work_dir.mkdir, parents=True, exist_ok=True)

    backend_label = acp_backends.descriptor_for(self.backend).label
    try:
        await asyncio.to_thread(
            spec_agent_guard.assert_agent_permitted,
            self._agent,
            backend_label,
            self._work_dir,
        )
    except spec_agent_guard.SpecAdapterAgentRefused as exc:
        raise AcpToolGateUnroutable(str(exc)) from exc

    try:
        await asyncio.to_thread(
            tool_gate.enforce,
            self.backend,
            self._work_dir,
        )
    except tool_gate.ToolGateUnroutable as exc:
        raise AcpToolGateUnroutable(str(exc)) from exc

    await AcpClient._spawn(self)


class SpecAdapterAcpClient(AcpClient):
    """ACP client whose adapter-only admission runs before process creation.

    The first-class Kiro provider instantiates :class:`AcpClient` directly, so
    it gains no adapter conditional, awaited step, or failure mode (H13). The
    provider selects this class only for public-spec backends.
    """

    __module__ = "kiro_crew.acp.client"

    _initialize_session = _spec_adapter_initialize_session
    _spawn = _spec_adapter_spawn


def client_type_for_backend(backend: str) -> type[AcpClient]:
    """Return the admitted client class for a canonical ACP backend."""
    if requires_spec_adapter_admission(backend):
        return SpecAdapterAcpClient
    return AcpClient


def requires_spec_adapter_admission(backend: str) -> bool:
    """Whether *backend* must use the public-spec pre-spawn admission path."""
    return acp_backends.is_spec_dialect(backend)
