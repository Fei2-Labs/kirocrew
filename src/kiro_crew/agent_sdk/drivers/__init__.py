"""Backend drivers -- the only layer permitted to import ``kiro_crew.acp``.

The driver modules answer the SDK's questions in plain Python data -- bools,
strings, tuples -- so that neither an ACP type nor an ACP import reaches the
layer above them. ``acp_registry`` also supplies closures for provider creation;
the concrete provider class never crosses the boundary.

The boundary is enforced by ``scripts/check_agent_sdk_boundary.py``. What each
later phase moves into this tree is recorded in
``docs/request-for-change/rfc-crew-agent-sdk-boundary.md`` §5.5.
"""

from __future__ import annotations

__all__: list[str] = []
