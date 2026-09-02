"""``GET /api/acp-backends`` — the backend registry, as the dashboard sees it.

The frontend must not carry its own copy of the capability table. If it did, the
Settings card's disclosure and `kirocrew doctor` could disagree about what a
backend supports, and the operator would have no way to tell which was right. One
source, served here.

Each row is the UNION of two owners, and it must stay one row from one handler on
ONE registered route. Registering the path twice is not a duplicate that logs
something: aiohttp resolves in registration order and the first match answers, so
a second registration silently DELETES the other payload — every field one side
owns simply stops arriving, and no test that calls a handler directly can see it.

* The REGISTRY half — `label`, `experimental`, `dialect`, `routing`,
  `capabilities`, `degraded_count`, `signin_command`, `selectable` — is derived
  here from `acp.backends.descriptor_for`.
* The MACHINE half — `policy_id`, `installed`, `missing_components`,
  `install_command`, `restart_required` — comes from
  :func:`kiro_crew.dashboard.handlers.acp_backend_status.install_snapshot`, which
  asks through the spawn's own resolvers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from aiohttp import web

from kiro_crew.acp import backends as acp_backends
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)
from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND
from kiro_crew.dashboard.handlers.kiro_prerequisite import _is_dashboard_owner
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_AUDIT_OPERATION = "acp_backend_status_access"


async def _audit_denial(request: web.Request, reason: str) -> None:
    """Record a refused read on the security event log; never raise.

    The payload names which backends are selectable and the active one's
    tool-gate verdict, so a refused attempt to read it is host-security-relevant
    and belongs on the audit log. An unwritable log must not turn the denial into
    a 500 -- the refusal is the security-relevant half and still has to land.
    """
    caller = str(request.get("user") or "")
    audit_caller = str(request.get("app") or caller or "unknown")

    def _log() -> None:
        sel().log_api_access(
            caller=audit_caller,
            operation=_AUDIT_OPERATION,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error=reason,
        )

    try:
        await asyncio.to_thread(_log)
    except Exception:
        logger.debug("Could not audit denied ACP backend read", exc_info=True)


def _probe_installed(
    backend: str,
    registry_adapters: Mapping[str, Any] | None = None,
    npm_resolution: Any = None,
) -> str:
    """Is this backend's adapter resolvable on this host?

    Answers through the SAME resolver the spawn uses, never a second
    reimplementation: a probe that agreed with a hand-rolled PATH search but
    disagreed with the spawn would tell the operator they are ready and then fail
    the session, which is worse than not checking.

    Three states, and the third is not padding. ``unknown`` means the check
    itself failed — never report ``missing`` on a failed check, because that
    tells someone to install what they may already have, and the remedy it
    implies (a global npm install) is not free.
    """
    try:
        if backend == ACP_BACKEND_CODEX:
            from kiro_crew.acp import codex

            return "installed" if codex.resolve_argv_cached() else "missing"
        if backend == ACP_BACKEND_CLAUDE:
            from kiro_crew.acp.client import _resolve_claude_acp_bin_cached

            return "installed" if _resolve_claude_acp_bin_cached() else "missing"
        if backend == ACP_BACKEND_KIRO:
            from kiro_crew.acp.client import _resolve_kiro_bin

            return "installed" if _resolve_kiro_bin() else "missing"
        if backend == ACP_BACKEND_GOOSE:
            # Answers through the SAME resolver the spawn now uses. This returned
            # "unknown" while goose had no spawn path, because any probe would have
            # been a second implementation that could disagree with the launch —
            # and two shortcuts were tried and rejected then: trusted_system_bin
            # excludes same-uid-writable dirs so it reports "missing" for a goose
            # in ~/.local/bin, and shutil.which answers from a PATH the spawn may
            # not share. Now there is one resolver to agree with.
            from kiro_crew.acp import goose

            return "installed" if goose.resolve_argv_cached() else "missing"
        if backend == ACP_BACKEND_OPENCODE:
            from kiro_crew.acp import opencode

            return "installed" if opencode.resolve_argv_cached() else "missing"
        if backend == ACP_BACKEND_PI:
            from kiro_crew.acp import pi

            return "installed" if pi.resolve_argv_cached() else "missing"
    except Exception:
        logger.debug("Install probe failed for %s", backend, exc_info=True)
        return "unknown"
    try:
        from kiro_crew.acp import registry

        adapters = registry.cached() if registry_adapters is None else registry_adapters
        adapter = adapters.get(backend)
        if adapter is not None and adapter.is_launchable:
            return "installed" if adapter.resolve_launch_argv(npm_resolution) else "missing"
    except Exception:
        logger.debug("Registry adapter probe failed for %s", backend, exc_info=True)
        return "unknown"
    # A backend with no resolver of its own (KAS is launched by the host, not
    # found on PATH). Saying "unknown" is honest; saying "missing" would invite
    # an install that does not exist.
    return "unknown"


def _descriptor_payload(
    backend: str,
    probe: bool = False,
    *,
    registry_adapters: Mapping[str, Any] | None = None,
    selectable: frozenset[str] | None = None,
    npm_resolution: Any = None,
    install_row: Mapping[str, Any] | None = None,
) -> dict:
    """One backend as JSON — the registry descriptor merged with *install_row*.

    Capability levels are sent as their string values rather than booleans so the
    UI can distinguish supported, degraded, unavailable and unverified. Collapsing
    them would make "works differently", "missing" and "not measured" render
    identically.

    Every machine-half key is present on every row, probed or not, because the
    client types them as required; unprobed they carry the "we did not look"
    value rather than being omitted, so a consumer never has to distinguish an
    absent key from a negative answer. ``policy_id`` is the exception that costs
    nothing to answer — it is a static translation, not a probe, so it is always
    the real one.
    """
    descriptor = acp_backends.descriptor_for(backend, registry_adapters=registry_adapters)
    capabilities = {
        capability: descriptor.capabilities[capability].value
        for capability in acp_backends.ALL_CAPABILITIES
    }
    differences = sum(1 for value in capabilities.values() if value != "supported")
    if selectable is None:
        selectable = acp_backends.selectable_ids()
    # "" when not probed, so the UI can tell "we did not look" apart from
    # "we looked and could not tell" (`unknown`).
    installed = ""
    if probe:
        installed = _probe_installed(
            backend,
            registry_adapters=registry_adapters,
            npm_resolution=npm_resolution,
        )
    install_command = descriptor.install_command
    missing_components: list[str] = []
    restart_required = False
    if install_row is not None:
        # The install snapshot is the only side that can NAME the absent
        # components, so its DEFINITE verdict wins where it has one. It has no
        # probe for most ids and answers `unknown` for them, which is exactly
        # where the adapter resolvers above are the better answer — taking
        # `unknown` here would erase a verdict the spawn's own resolver gave.
        snapshot_installed = str(install_row.get("installed") or "")
        if snapshot_installed in ("installed", "missing"):
            installed = snapshot_installed
            missing_components = list(install_row.get("missing_components") or [])
            restart_required = bool(install_row.get("restart_required"))
        install_command = str(install_row.get("install_command") or "") or install_command
    return {
        "id": descriptor.id,
        # The kiro backend's id is "" in code, so it cannot be its own wire
        # name. An unregistered id falls back to itself rather than to "", so a
        # registry adapter still sorts and renders under a name.
        "policy_id": str(POLICY_ID_BY_BACKEND.get(descriptor.id, descriptor.id)),
        "label": descriptor.label,
        "experimental": descriptor.experimental,
        "selectable": descriptor.id in selectable,
        "signin_command": descriptor.signin_command,
        "install_command": install_command,
        "dialect": descriptor.dialect.value,
        "routing": descriptor.routing.value,
        "capabilities": capabilities,
        "degraded_count": differences,
        "installed": installed,
        "missing_components": missing_components,
        "restart_required": restart_required,
    }


def _active_state(
    selectable: frozenset[str] | None = None,
    registry_adapters: Mapping[str, Any] | None = None,
) -> dict:
    """The configured backend plus its routing verdict.

    The verdict is included so the card can show the same tool-gate status the
    doctor does. ``routing_verdict`` is read-only: a Settings load must not
    seed Claude/OpenCode config under ``config_dir()/workspace``. Resolved
    defensively: a probe failure must degrade the row, not fail the whole
    endpoint and leave the card with no data at all.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        cfg = KiroCrewConfig.load()
        active = cfg.agent.acp_backend or ""
        allow_ungated = bool(cfg.agent.acp_backend_allow_ungated_tools)
    except Exception:
        logger.debug("Could not read the ACP backend config", exc_info=True)
        return {"active": "", "allow_ungated_tools": False, "routing_verdict": ""}

    verdict = ""
    reason = ""
    if active:
        try:
            from kiro_crew.acp import tool_gate
            from kiro_crew.config.paths import config_dir

            if registry_adapters is None:
                resolved, reason = tool_gate.routing_verdict(
                    active,
                    config_dir() / "workspace",
                )
            else:
                resolved, reason = tool_gate.routing_verdict(
                    active,
                    config_dir() / "workspace",
                    registry_adapters=registry_adapters,
                )
            verdict = resolved.value
        except Exception:
            logger.debug("Could not resolve the tool-gate verdict", exc_info=True)

    return {
        "active": active,
        "allow_ungated_tools": allow_ungated,
        "routing_verdict": verdict,
        "routing_reason": reason,
    }


async def api_acp_backends(request: web.Request) -> web.Response:
    """GET /api/acp-backends — registry, active selection, and routing verdict.

    Owner-only. The payload names which backends are selectable and reports the
    active one's tool-gate verdict, both of which describe the security posture of
    the host; a non-owner viewer has no reason to read either.
    """
    if request.get("app") != "":
        await _audit_denial(request, "app scope not permitted")
        return web.json_response(
            {"error": "app scope not permitted", "code": "app_scope_forbidden"},
            status=403,
        )
    if not _is_dashboard_owner(request):
        await _audit_denial(request, "owner only")
        return web.json_response(
            {"error": "owner only", "code": "owner_only"},
            status=403,
        )

    # Install detection is OPT-IN via `?probe=1`, never the default. It walks the
    # resolution ladder for every backend — filesystem stats and PATH scans — and
    # the caller that wants it is the Settings card, which only renders when the
    # operator has enabled the ACP-backends preview flag. A client that has not
    # opted in must not pay for a probe whose answer it will not show. The flag
    # itself is per-device browser state the Gateway cannot read, so the query
    # param is where that intent crosses the wire.
    probe = request.query.get("probe") in ("1", "true")
    from kiro_crew.acp import registry

    registry_adapters = await asyncio.to_thread(registry.fetch)
    backend_ids = acp_backends.selectable_ids()
    described_ids = acp_backends.known_ids() | frozenset(
        acp_backends.canonical_backend_id(adapter_id) for adapter_id in registry_adapters
    )
    # Refresh precedes config normalization. On first discovery, reading active
    # state against the old cache would degrade a persisted dynamic adapter to
    # Kiro while the same response lists that adapter as selectable.
    state = await asyncio.to_thread(_active_state, backend_ids, registry_adapters)
    npm_resolution = None
    if probe and any(adapter.is_launchable for adapter in registry_adapters.values()):
        npm_resolution = await asyncio.to_thread(registry.npm_resolution_snapshot)
    # Part of the same opt-in: the snapshot shells out to mise and walks the
    # filesystem, so an unprobed caller must not pay for it.
    install_rows: Mapping[str, Mapping[str, Any]] = {}
    if probe:
        from kiro_crew.dashboard.handlers.acp_backend_status import install_snapshot

        try:
            install_rows = await asyncio.to_thread(install_snapshot)
        except Exception:
            # Same defensive posture as ``_active_state``: a failed probe
            # degrades the machine half of the rows, it must never cost the
            # card its whole payload.
            logger.debug("Could not read the backend install snapshot", exc_info=True)
    backends_payload = await asyncio.to_thread(
        lambda: [
            _descriptor_payload(
                backend,
                probe=probe,
                registry_adapters=registry_adapters,
                selectable=backend_ids,
                npm_resolution=npm_resolution,
                install_row=install_rows.get(backend),
            )
            for backend in sorted(described_ids)
        ]
    )
    return web.json_response({**state, "probed": probe, "backends": backends_payload})


__all__ = ["api_acp_backends"]
