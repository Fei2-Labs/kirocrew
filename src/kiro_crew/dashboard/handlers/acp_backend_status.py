"""This machine's per-backend install snapshot, as ``GET /api/acp-backends`` rows.

One entry per backend id in ``acp_backends.ACP_BACKENDS_KNOWN``, including ids
this build cannot serve: the dashboard's backend switch lists all of them and
must be able to say which is which, so an unservable backend needs a row rather
than silent absence.

This module owns the MACHINE half of that endpoint's rows -- whether the harness
is present on THIS host, which components are absent, and whether the running
gateway has already cached that absence. The registry/descriptor half (labels,
capabilities, dialect, routing) and the endpoint itself live in
:mod:`kiro_crew.dashboard.handlers.acp_backends`, which merges the two. There is
exactly ONE handler and ONE route for the path; a second registration made the
richer payload unreachable once already.

``installed`` is read through :mod:`kiro_crew.agent_sdk`, which asks through the
spawn's own resolvers. Reached through the SDK rather than the ACP layer
directly: this handler is application code, and
``scripts/check_agent_sdk_boundary.py`` is what keeps that true.

BLOCKING: the Claude probe shells out to mise and walks the filesystem, so every
caller must offload it. That cost is also why the endpoint only reaches here
under an explicit ``?probe=1``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def install_snapshot() -> Dict[str, Dict[str, Any]]:
    """Per-backend install facts, keyed by backend id. BLOCKING -- offload it.

    ``kiro_crew.agent_sdk`` is imported here rather than at module scope: the
    route table imports this module's package on the boot path, and the probe
    reaches the ACP driver, so a module-scope import would drag the ACP client
    and runtime into gateway start (pinned by
    ``test_the_boot_path_does_not_import_acp_at_module_scope``).
    """
    from kiro_crew.agent_sdk import INSTALLED, MISSING, probe_backends

    snapshot: Dict[str, Dict[str, Any]] = {}
    for state in probe_backends():
        snapshot[state.backend] = {
            "policy_id": state.policy_id,
            "installed": state.installed,
            # Enforced here, not just by the probes: the contract makes this
            # non-empty ONLY for a MISSING verdict, so an UNKNOWN row can
            # never name a component the check never confirmed was absent.
            "missing_components": (
                list(state.missing_components) if state.installed == MISSING else []
            ),
            "install_command": state.install_command,
            # Clamped to a MISSING-free verdict for the same reason as
            # ``missing_components`` above: "installed but the running gateway
            # cannot use it yet" is only meaningful once the components are
            # actually there. A MISSING or UNKNOWN row carries False.
            "restart_required": (
                bool(state.restart_required) if state.installed == INSTALLED else False
            ),
        }
    return snapshot


__all__ = ["install_snapshot"]
