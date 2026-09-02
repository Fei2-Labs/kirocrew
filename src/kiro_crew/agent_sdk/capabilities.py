"""Semantic backend questions, asked without naming a backend or a capability key.

The boundary's rule is that a consumer asks what it needs to KNOW, never what
the backend IS: no consumer branches on a backend id, and none of them handles
the ACP layer's capability keys either. A ``supports(backend, CAP_X)`` pair
re-exported through here would satisfy the import gate while leaving the branch
itself in application code, which is the shape the RFC rejects -- so each
question lands as its own named predicate with its own fail-closed contract, and
the key stays behind the driver.

These are PRE-SESSION questions: they are asked in order to decide how to build
a session, so no value read off a live session can answer them. That is the same
split the RFC draws for ``spawnable_multiplexed_selections`` -- post-session
questions belong on ``SessionCapabilities`` instead.

Computed per call, never frozen at import: a backend's descriptor can arrive
from the ACP registry cache after this module is imported, so an answer cached
at import time would be a different answer than the spawn's.

Design of record: ``docs/request-for-change/rfc-crew-agent-sdk-boundary.md``.
"""

from __future__ import annotations

from kiro_crew.agent_sdk.drivers import acp as _driver

__all__ = ["backend_uses_registry_model_ids"]


def backend_uses_registry_model_ids(backend: str) -> bool:
    """True when ``backend`` takes model ids in Kiro Crew's own registry spelling.

    False for a backend whose support is degraded, absent, or simply unmeasured:
    the caller then passes the served id straight through instead of mapping it,
    and an unmapped id that the backend happens to accept is a far cheaper
    outcome than a mapped id it rejects on the first prompt.

    An id no backend descriptor covers raises rather than answering False, so a
    misspelled ``agent.acp_backend`` surfaces instead of quietly disabling every
    model mapping.
    """
    return _driver.backend_uses_registry_model_ids(backend)
