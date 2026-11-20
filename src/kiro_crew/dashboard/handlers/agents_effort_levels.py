"""Dashboard model-picker helpers and ``GET /api/effort-levels``.

:mod:`kiro_crew.dashboard.handlers.agents` binds this name and is the import path
callers use; this plain module sits outside the owner composition, as
``agents_cc_model_rows`` and ``agents_harness_effort`` do.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.agent_sdk.backends import backend_from_stored
from kiro_crew.agent_sdk.drivers.acp_registry import backend_speaks_kiro_dialect
from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_ordered
from kiro_crew.dashboard.chat_utils import _history_key_for


def _provider_backend(provider: object | None) -> str | None:
    """ACP backend a live provider reports through the provider contract."""
    if provider is None:
        return None
    try:
        backend = getattr(provider, "backend")
    except (AttributeError, TypeError):
        return None
    return backend if isinstance(backend, str) else None


def _configured_acp_backend() -> str:
    """Configured non-default ACP backend, or ``""`` for kiro-cli."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        backend = KiroCrewConfig.load().agent.acp_backend
        if not isinstance(backend, str):
            return ""
        # Stored "kiro" is the config spelling for the runtime id ``""``.
        resolved = backend_from_stored(backend)
        return resolved if isinstance(resolved, str) else ""
    except Exception:
        return ""


def _effort_levels_for(request: web.Request) -> list[str]:
    """Return advertised effort levels without borrowing kiro's list for adapters."""
    slot = ""
    try:
        raw_slot = request.query.get("slot")
        slot = raw_slot.strip() if isinstance(raw_slot, str) else ""
    except Exception:
        slot = ""
    if slot:
        try:
            state = request.app.get("state")
            sessions = getattr(state, "sessions", None)
            getter = getattr(sessions, "get_provider", None)
            provider = getter(_history_key_for(slot)) if callable(getter) else None
        except Exception:
            provider = None
        live_backend = None
        if provider is not None:
            try:
                getter = getattr(provider, "get_valid_effort_levels", None)
                levels = getter() if callable(getter) else []
            except (AttributeError, KeyError):
                levels = []
            if levels:
                return list(levels)
            live_backend = _provider_backend(provider)
        # A live provider that names its backend decides. A cold slot (no live
        # provider yet) or a provider that names none falls to the configured
        # backend below, as the settings path does.
        if live_backend is not None:
            return (
                get_reasoning_effort_ordered() if backend_speaks_kiro_dialect(live_backend) else []
            )

    backend = _configured_acp_backend()
    if backend and not backend_speaks_kiro_dialect(backend):
        return []
    return get_reasoning_effort_ordered()


async def api_effort_levels(request: web.Request) -> web.Response:
    """GET /api/effort-levels — list available reasoning effort levels.

    A slot-scoped adapter with no advertised levels stays empty. The Kiro
    dialect keeps the process-global fallback for cold starts and sessions that
    have not reported their levels.
    """
    return web.json_response(_effort_levels_for(request))
