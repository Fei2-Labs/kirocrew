"""Construct a provider for an adapter selected by the platform registry."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def create_adapter_provider_factory(
    config: Any,
    *,
    factory_backend: str,
    provider_type: type,
    registry_model_ids: bool,
    tool_search_supported: bool,
    default_model: str,
    session_work_dir: Callable[..., Path],
    capabilities_for: Callable[..., Any],
    default_overlay_dir: Callable[[], str | Path],
    default_socket_path: Callable[[], str | Path],
    is_valid_effort: Callable[..., bool],
    logger: Any,
    model_supports_effort: Callable[..., bool],
    resolve_cc_permission_mode: Callable[..., str | None],
    resolve_crew_identity: Callable[..., str],
    acp_backends_effort_from_advertised_option: frozenset[str],
) -> Callable:
    """Build one registry-selected non-Kiro ACP provider factory.

    Backend identity, provider class, and capabilities are inputs owned by
    ``ProviderRegistry``. The first-class factory above never calls this
    adapter-only helper.
    """
    model = config.agent.model
    if registry_model_ids and model == default_model:
        model = config._resolve_agent_model()

    sandbox = config.agent.sandbox
    tool_search = config.agent.tool_search
    tool_search_min_pct = config.agent.tool_search_min_pct
    tool_search_min_tokens = config.agent.tool_search_min_tokens

    # MCP gateway: resolve overlay + socket once, iff some server is stubbed
    # through the gateway. Routing is what puts a stub in the path, and the
    # stub is what carries both the render/callback path and any sharing —
    # so an empty stub set means no stub, no daemon, and no gateway in the
    # path at all (AcpClient falls through to per-session MCP). Sharing
    # (``enabled``) is not consulted here: it decides how a stubbed server's
    # backend is ACQUIRED, and on its own routes nothing.
    _gw = config.mcp_gateway
    if _gw.stub_servers:
        _gw_overlay = _gw.overlay_dir or str(default_overlay_dir())
        _gw_socket = _gw.socket_path or str(default_socket_path())
    else:
        _gw_overlay = None
        _gw_socket = None

    # Effort-drop warnings already emitted by this factory, keyed by
    # (resolved model, level) — see the gate below. Benign under threads:
    # a lost race duplicates one log line, never drops state.
    _effort_drop_warned: set[tuple[str, str]] = set()

    def _acp(
        session_key: str | None = None,
        agent: str | None = None,
        channel_id: str | None = None,
        model_override: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        reasoning_effort_override: str | None = None,
        crew_agent: str | None = None,
        inherit_config_model: bool = True,
        # Per-session opt-in for the claude backend's own permission
        # classifier. NAMED rather than left to ``**_kwargs`` on purpose: a
        # caller passing it into the catch-all would be swallowed here and
        # the session would spawn on the backend's default with no error.
        permission_mode: str | None = None,
        shared_scratch: Path | None = None,
        **_kwargs: object,
    ) -> Any:
        wdir = Path(cwd) if cwd else session_work_dir(session_key)
        # Canonical crew identity for the session (keys per-agent watchdog
        # windows on the handle) — one shared resolution rule, see
        # resolve_crew_identity.
        crew_agent = resolve_crew_identity(config, agent, crew_agent)
        # Kiro-owned model ids follow the same override > named Kiro agent
        # pin > global precedence as the first-class factory. A spec
        # adapter's namespace is independent: only an explicit override or
        # concrete global is meaningful there, and auto stays the adapter's
        # default. Reading Kiro agent JSON for that path would leak a valid
        # but foreign id onto the adapter wire.
        # "" at the end means nothing is pinned anywhere; AcpClient
        # normalizes "" to default_model, same as None.
        # Selection and the optional registry translation live in
        # acp_effective_model — SHARED with the spawn-side effort verdict
        # (subagent.py) so the reported outcome cannot drift from what this
        # gate actually keys on. (Why the translation is keyed on the
        # backend, and why to_acp_id is the non-claude choice, is documented
        # on that method.)
        # The registry selected and validated this adapter before creating
        # this backend-bound factory. Do not pass it through the direct-Kiro
        # selector again: that selector intentionally degrades registry-only
        # adapters, turning a Pi factory into Kiro before model and effort
        # namespace resolution. Per-session crossover swaps factories in
        # session_allocation rather than changing this bound identity.
        _backend = factory_backend
        # Resolved BEFORE the model, and threaded into the resolution: the
        # model's namespace translation and its pin-scope check both have to
        # key on the backend this session actually gets, not on the
        # configured default. A member-DM thread auto-routed to another
        # harness runs a backend the config does not name, so keying either
        # on the configured field judges the pin for a namespace that
        # session never reaches.
        # ``inherit_config_model=False`` is the caller saying this session is
        # NOT the configured harness, so the configured harness's own pins are
        # not its inheritance: an explicit override still applies, and
        # everything below it defers to the adapter's default. Honoured here
        # rather than trusted to namespace scoping alone, because scoping
        # cannot object to a pin whose harness has advertised nothing (see
        # model_scope: silence is not a refusal), and that is exactly the
        # crossover case this flag exists for.
        m = config.acp_effective_model(
            agent if inherit_config_model else None,
            model_override,
            global_model=model if inherit_config_model else "",
            namespace=capabilities_for(_backend).model_id_namespace,
        )
        # Thread the slot's effort into a per-model override so the kiro
        # cli.json overlay is written from it at spawn — without this, a
        # kiro cold start (or the handler's reset-then-respawn) would only
        # pick up effort already recovered from a pre-existing overlay,
        # never the freshly-set slot value. Mirrors the _claude_code path.
        _eff_per_model: dict[str, str] = {}
        # Everything below an explicit override — the crew's pin, then the
        # role-aware default — resolves in resolve_session_effort, which the
        # crews API also serves its readout from. An explicit override (the
        # dashboard slot's effort, or a sub-agent's resolved "subagent"
        # effort) still wins over all of it.
        _eff = reasoning_effort_override or config.resolve_session_effort(agent, crew_agent)
        # On a harness whose effort capability and vocabulary come from the
        # option it ADVERTISES, neither check below can answer here. This
        # factory runs before any session exists, the registry carries none of
        # the operator's own model ids, and Crew's ladder does not list every
        # level such a harness offers -- so both facts arrive with
        # ``session/new``, and ``AcpProvider._resolve_effort`` validates the
        # level against the advertised list once they do. The level is carried
        # forward for a member and judged there. Judging it HERE drops it on
        # every cold start, and the session then runs the adapter's own default
        # while the dashboard still shows the level the operator picked.
        _from_option = _backend in acp_backends_effort_from_advertised_option
        _registry_ok = is_valid_effort(_eff) and model_supports_effort(m)
        if m and _eff and (_from_option or _registry_ok):
            _eff_per_model[m] = _eff
        elif _eff and is_valid_effort(_eff):
            # Single-authority drop warning: a valid requested effort is
            # being dropped because the resolved model is empty or not
            # effort-capable. Every surface (spawn, dashboard slot, cron)
            # funnels through this factory, so one log at the gate covers
            # them all and cannot drift from the decision it reports on.
            # Reporting-only — the overlay simply stays unwritten, exactly
            # as before. An unresolved model is named "auto" (it IS the
            # default_model sentinel the backend resolves itself), matching
            # the spawn-side effort_dropped verdict so one drop event reads
            # as one event across both surfaces.
            #
            # An EXPLICIT override always warns: a caller's own request
            # being dropped is the event this gate exists to surface, and
            # a config-default drop must not burn its dedupe key first
            # (Design review on this PR). Only the static config default
            # (base_effort with no override) dedupes per (model, level) —
            # it is one unchanging configuration fact that would otherwise
            # repeat on every provider construction (warm-pool fills and
            # recycles included); a config change rebuilds the factory and
            # re-arms it.
            _dedupe = not reasoning_effort_override
            if not _dedupe or (m, _eff) not in _effort_drop_warned:
                if _dedupe:
                    _effort_drop_warned.add((m, _eff))
                logger.warning(
                    "reasoning effort '%s' will not be applied (session %s) — "
                    "model '%s' does not support effort configuration",
                    _eff,
                    session_key or "?",
                    m or "auto",
                )
        # No second selection gate here: this factory is the ADAPTER half,
        # reached only after the caller resolved the backend and paired it
        # with the matching ``provider_type``. Selecting again would let the
        # two disagree — a SPEC id constructed through the Kiro provider
        # contract, which is the catch-all inheritance H5 forbids. The one
        # per-session selection runs in the direct Kiro factory above.
        return provider_type(
            work_dir=wdir,
            model=m,
            agent=agent,
            crew_agent=crew_agent,
            sandbox_mode=sandbox,
            session_key=session_key,
            channel_id=channel_id,
            extra_env=extra_env,
            acp_backend=factory_backend,
            effort_per_model=_eff_per_model,
            # None means "do not write the overlay at all", which is the
            # correct request for a backend that does not read kiro-cli's
            # cli.json. Passing False would write an explicit disable.
            tool_search=(tool_search if tool_search_supported else None),
            tool_search_min_pct=tool_search_min_pct,
            tool_search_min_tokens=tool_search_min_tokens,
            mcp_gateway_overlay=_gw_overlay,
            mcp_gateway_socket=_gw_socket,
            permission_mode=resolve_cc_permission_mode(permission_mode, _backend),
            # A dedicated subagent process joins its parent's session tree:
            # the tree's work directory is mounted beside its own scratch
            # and is what its ``$KIROCREW_SCRATCH`` names (agent_scratch).
            shared_scratch=shared_scratch,
        )

    return _acp
