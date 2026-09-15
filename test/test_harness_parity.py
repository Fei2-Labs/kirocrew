"""Structural pins for the harness-parity invariants.

Kiro Crew drives one first-class harness, ``kiro-cli``, and adapts the others.
Each test here closes one invariant from
``docs/system-specs/modules/harness-parity.md`` by its id, so a change that
degrades the Kiro path goes red here rather than at an operator's first message.

H14 is a property of a *change* rather than of a tree and has no deterministic
form; it is carried by the ``harness-parity`` rule in ``AUTOSDE.yaml``. H13's
direct Kiro-factory half is pinned in ``test_provider_dispatch.py``. The
added-line half of H5 lives in
``scripts/check_harness_parity.py`` and is exercised by its ``--test`` mode,
which :func:`test_added_line_gate_self_test_passes` runs.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import subprocess
import sys
import textwrap
from dataclasses import fields
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp import types as acp_types
from kiro_crew.acp.harness import KasHarness, KiroHarness
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_AUTO_MODEL,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KIRO_CREDITS,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MODEL_NAMESPACE,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
    ACP_CLIENT_CAPABILITIES,
    KAS_CLIENT_CAPABILITIES,
    PROVIDER_LABEL_CODEX,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABELS_BY_BACKEND,
)
from kiro_crew.acp_backends import (
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    BASELINE_SELECTABLE_BACKENDS,
    selectable_backends,
)
from kiro_crew.agent_sdk import backends as acp_backends
from kiro_crew.config.loader import AgentConfig, _normalize_acp_backend
from kiro_crew.providers import acp as providers_acp
from kiro_crew.providers import mirrors

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GATE_PATH = os.path.join(_REPO_ROOT, "scripts", "check_harness_parity.py")


def _field_default(name: str) -> object:
    for f in fields(AgentConfig):
        if f.name == name:
            return f.default
    raise AssertionError(f"AgentConfig has no field {name!r}")


def _field_enum(name: str) -> object:
    for f in fields(AgentConfig):
        if f.name == name:
            return f.metadata.get("enum")
    raise AssertionError(f"AgentConfig has no field {name!r}")


# ---------------------------------------------------------------------------
# Group A: Kiro is the default and the floor
# ---------------------------------------------------------------------------


def test_kiro_is_the_default_backend() -> None:
    """H1: configuring nothing yields the Kiro harness."""
    assert _field_default("acp_backend") == ACP_BACKEND_KIRO


def test_kiro_is_always_selectable() -> None:
    """H1: the Kiro harness is never gated behind a preview flag or an edition.

    Every other member is a policy decision; this one is the floor. Without it
    an operator can persist a configuration in which no harness is selectable.

    Reads the registry, not a frozen constant: the selectable set is now extended
    at boot by an edition, so a snapshot taken at import would not be the set the
    dashboard offers. The floor is a property of the BASELINE, which is what makes
    it independent of whatever an edition registers on top.
    """
    assert ACP_BACKEND_KIRO in BASELINE_SELECTABLE_BACKENDS
    assert ACP_BACKEND_KIRO in selectable_backends()


def test_initial_adapter_selection_is_limited_to_reviewed_backends() -> None:
    """H1: discovery does not advertise an adapter with no install probe.

    Claude IS admitted: ``acp/client.py`` owns its whole spawn path, the adapter is
    a public npm package, and ``agent_sdk/backend_install.py`` probes for it — so
    the switch has something to say when a session fails to start. Adopted from
    upstream at the 2026-09-04 sync.

    Codex, goose and pi stay withheld, and the reason is recorded once in
    ``acp_backends.NOT_SHIPPED_SELECTABLE``: ``_PROBES`` has no entry for them, so
    their readiness verdict is ``UNKNOWN`` with nothing an operator could act on.

    FORK DIVERGENCE: the fork additionally admits ``ACP_BACKEND_COPILOT`` and
    ``ACP_BACKEND_OPENCODE`` — the fork's core resolves and spawns
    ``copilot --acp`` / ``opencode acp`` directly and both have been driven end to
    end here.

    Admission entitles a backend to no capability set; every ``ACP_BACKENDS_*``
    membership stays a separate opt-in decision (H6).
    """
    assert selectable_backends() == frozenset(
        {
            ACP_BACKEND_KIRO,
            ACP_BACKEND_KAS,
            ACP_BACKEND_CLAUDE,
            # Selectable since the 2026-09-14 sync: upstream added an install probe
            # for it, which is the criterion NOT_SHIPPED_SELECTABLE names.
            ACP_BACKEND_CODEX,
            ACP_BACKEND_COPILOT,
            ACP_BACKEND_OPENCODE,
        }
    )
    # The withheld ids are withheld by being NAMED, not by omission.
    assert selectable_backends() == acp_backends.ACP_BACKENDS_KNOWN - (
        acp_backends.NOT_SHIPPED_SELECTABLE
    )


def test_provider_enum_is_acp_only() -> None:
    """H2: a harness is chosen at ``acp_backend``, never as a second provider.

    A second ``agent.provider`` value would build its factory outside
    ``create_provider_factory`` and route around every invariant below it.
    """
    assert _field_enum("provider") == ["acp"]
    assert _field_default("provider") == "acp"


@pytest.mark.parametrize(
    "persisted",
    ["", "kas", "byo-harness", "claude", "codex", "codex-acp", None, 7],
)
def test_unselectable_backend_degrades_to_kiro(persisted: object) -> None:
    """H3: an unusable persisted value degrades to Kiro and never raises.

    Includes the non-string shapes a hand-edited config.json can hold: a gate
    that raises here turns a typo into a gateway that will not boot.

    ``claude`` is in the list on purpose, and now for the opposite reason: it ships in
    the public baseline, so it must SURVIVE rather than degrade. The assertion below is
    conditional on membership precisely so this row proves the gate reads the registry
    instead of hardcoding a verdict. ``byo-harness`` covers the unknown-id case, and a
    known id that policy has denied is covered in
    ``test_agent_backend_governance.py``.
    """
    resolved = _normalize_acp_backend(persisted)
    assert resolved in selectable_backends()
    if persisted not in selectable_backends():
        assert resolved == ACP_BACKEND_KIRO


def test_registering_a_backend_makes_it_survive_load() -> None:
    """H3 + H8: the gate reads the registry per call, so registration is the seam.

    This is the whole point of the registry: an edition calls
    ``register_selectable_backend`` and the SAME persisted value that degraded a
    moment ago now survives, with no second gate and no code change anywhere else.
    Ordering is the edition's to get right -- registration must precede the first
    config load.

    Claude Code is in the baseline (adopted from upstream at the 2026-09-04 sync),
    so the degrading starting state is CONSTRUCTED by the discards below rather than
    borrowed from an unshipped id. The post-restore assertion reads the snapshot, so
    it stays correct whichever backends the build ships.
    Both module sets are snapshotted: ``register_selectable_backend`` writes
    the baseline too, and restoring only the effective set would leak a widened
    baseline into the rest of the run.
    """
    # Reaches the private registry state through ``agent_sdk.backends``, the module
    # that DEFINES it. The ``kiro_crew.acp_backends`` shim re-exports the public
    # names only: a second binding to a mutable set is how two views of one
    # registry start disagreeing, so the private pair deliberately has one home.
    baseline_before = set(acp_backends._baseline)
    before = set(acp_backends._selectable)
    try:
        acp_backends._baseline.discard(ACP_BACKEND_CLAUDE)
        acp_backends._selectable.discard(ACP_BACKEND_CLAUDE)
        assert _normalize_acp_backend(ACP_BACKEND_CLAUDE) == ACP_BACKEND_KIRO

        acp_backends.register_selectable_backend(ACP_BACKEND_CLAUDE)
        assert _normalize_acp_backend(ACP_BACKEND_CLAUDE) == ACP_BACKEND_CLAUDE
    finally:
        acp_backends._baseline.clear()
        acp_backends._baseline.update(baseline_before)
        acp_backends._selectable.clear()
        acp_backends._selectable.update(before)
    # The restore really restored: the gate reads the registry per call, so claude
    # normalizes to itself iff it is selectable again. Expressed against the
    # snapshot rather than a literal, so it holds whichever backends the build ships.
    expected = ACP_BACKEND_CLAUDE if ACP_BACKEND_CLAUDE in before else ACP_BACKEND_KIRO
    assert _normalize_acp_backend(ACP_BACKEND_CLAUDE) == expected


def test_config_load_never_reads_the_platform_context(monkeypatch) -> None:
    """H3: the load path must not reach the platform context, at all.

    ``current_context()``'s lazy branch LOADS CONFIG, so any lookup that reaches it
    from inside ``KiroCrewConfig.load()`` re-enters that load and recurses to the
    stack limit — and a broad ``except`` around it does not save the caller, it
    downgrades the crash to a silently wrong backend.

    Nothing in the current load path reaches it, which is exactly why this guard is
    worth pinning: the natural next feature here is a per-deployment policy on which
    backend may run, and resolving a policy is precisely the call that would
    reintroduce the cycle.

    RECORDS the reach with a spy rather than raising on it. A raising stub cannot
    prove this: ``resolve_selected_backend``'s callers catch broadly, so an
    ``AssertionError`` is swallowed and the fallback returns the value the test
    would then assert — passing against the very implementation it rejects.
    """
    from kiro_crew.platform import context as pc

    reached: list = []
    monkeypatch.setattr(pc, "current_context", lambda: reached.append("current_context"))
    monkeypatch.setattr(pc, "installed_context", lambda: reached.append("installed_context"))

    for value in ("", "kas", "byo-harness", "claude", None, 7):
        assert _normalize_acp_backend(value) in ACP_BACKENDS_KNOWN

    assert reached == [], f"config normalization reached the platform context: {reached}"


def test_selectability_has_one_logged_gate() -> None:
    """H4: ``resolve_selected_backend`` is the ONLY gate, and it logs.

    This replaces the previous two-mechanism guarantee, deliberately. The old
    contract kept a static ``enum`` on the field as a second, SILENT gate:
    ``validate_config_data`` deletes an out-of-enum value before the loader sees
    it, and the degrade log only fires on a non-empty value, so a backend an
    edition had legitimately registered was stripped from config.json with no log
    line at all — the exact failure the old H4 text described as a hazard and did
    not prevent. Removing the enum makes the logged degrade the single gate.

    Pinned here rather than left to prose because re-adding ``enum=`` would look
    like a harmless tidy-up and would silently restore the strip.
    """
    assert _field_enum("acp_backend") is None, (
        "acp_backend must NOT declare a static enum: it is frozen at import, "
        "before an edition registers its backends, and validate_config_data "
        "deletes out-of-enum values silently"
    )


# ---------------------------------------------------------------------------
# Group B: identity is tested positively
# ---------------------------------------------------------------------------


def test_session_sharing_is_opt_in() -> None:
    """H6: session-sharing eligibility is membership, not the absence of claude.

    The property must read the set, so a harness added to ``ACP_BACKENDS_KNOWN``
    and nowhere else is ineligible by default instead of inheriting eligibility.
    """
    source = inspect.getsource(providers_acp.AcpProvider.is_session_sharing_eligible.fget)
    assert "ACP_BACKENDS_SESSION_SHARING" in source
    assert "not " not in source.split('"""')[-1], "eligibility derived from a negation"

    assert ACP_BACKEND_KIRO in ACP_BACKENDS_SESSION_SHARING
    # claude-agent-acp runs one process per session (AcpClient), so it cannot
    # host a multiplexed subagent session however the call site is written.
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_SESSION_SHARING


def test_steer_is_opt_in() -> None:
    """H6: the ``_session/steer`` extension is claimed by membership."""
    source = inspect.getsource(acp_client.AcpClient.supports_steer.fget)
    assert "ACP_BACKENDS_STEER" in source
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_STEER
    assert ACP_BACKEND_KAS in ACP_BACKENDS_STEER
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_STEER
    assert ACP_BACKEND_GOOSE not in ACP_BACKENDS_STEER
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_STEER
    assert ACP_BACKEND_OPENCODE not in ACP_BACKENDS_STEER
    assert ACP_BACKEND_PI not in ACP_BACKENDS_STEER


def test_mcp_config_hot_reload_is_opt_in() -> None:
    """H6: skipping the post-sync session reset is claimed by membership.

    The gate must read the set — a harness added to ``ACP_BACKENDS_KNOWN`` must
    not inherit the skip, because a wrong member leaves a freshly installed
    server unmounted with nothing red to say why. KAS receives its servers on
    ``session/new`` and claude reads no agent file, so neither is a member.
    """
    from kiro_crew import mcp_hot_reload

    source = inspect.getsource(mcp_hot_reload.mcp_hot_reload_supported)
    assert "ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD" in source
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD
    assert ACP_BACKEND_KAS not in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD


def test_steer_capability_declares_its_stamp() -> None:
    """H15: a provider that can steer must also report WHEN it steered.

    The pair is load-bearing because the failure of the second half is silent.
    A sleeping ``wait`` is one of the few regions where a steer cannot be
    injected — the backend needs a model-inference boundary and an in-flight
    tool call is the absence of one — so the keepalive route ends the sleep by
    comparing ``last_steer_monotonic`` against the reading taken when the sleep
    began. A provider that overrides ``supports_steer`` and inherits the default
    stamp accepts steers correctly and never interrupts a wait, with nothing
    raised and nothing logged.

    The route reads the stamp defensively (a keepalive must not fail on the ping
    that stops the watchdog killing the session mid-sleep), which is exactly why
    the guarantee has to live here instead: a defensive read cannot tell "this
    backend does not steer" from "this backend forgot the stamp".
    """
    from kiro_crew.acp.session_provider import AcpSessionProvider  # noqa: F401
    from kiro_crew.providers.acp import AcpProvider  # noqa: F401
    from kiro_crew.providers.base import LLMProvider

    def _walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from _walk(sub)

    checked = []
    for cls in _walk(LLMProvider):
        if cls.supports_steer is LLMProvider.supports_steer:
            continue  # cannot steer, so has nothing to stamp
        assert cls.last_steer_monotonic is not LLMProvider.last_steer_monotonic, (
            f"{cls.__name__} overrides supports_steer but inherits the default "
            "last_steer_monotonic, so a steer it accepts can never end a sleeping wait"
        )
        checked.append(cls.__name__)

    # Fail-closed: an import that stopped registering the subclasses would make
    # the loop vacuous and the ratchet a no-op.
    assert len(checked) >= 2, f"expected at least 2 steer-capable providers, saw {checked}"


def test_is_kiro_cli_is_positive() -> None:
    """H7: the sandbox-delegation flag is membership at every spawn site.

    This is the one identity test that fails OPEN. ``wrap_argv`` treats it as
    "this harness carries its own internal sandbox, which cannot nest inside
    ours, so skip ours" — granted to a harness without one, it leaves the agent
    process unconfined. A negative form grants it to every future harness.
    """
    for spawn in (acp_runtime.AcpRuntime.spawn, acp_client.AcpClient.ensure_ready):
        source = inspect.getsource(spawn)
        for line in source.splitlines():
            if "is_kiro_cli=" not in line:
                continue
            value = line.split("is_kiro_cli=", 1)[1]
            assert (
                "not " not in value and "!=" not in value
            ), f"{spawn.__qualname__} derives is_kiro_cli from a negation: {line.strip()}"
            assert "ACP_BACKENDS_INTERNAL_SANDBOX" in value or value.strip().startswith(
                ("True", "False")
            ), f"{spawn.__qualname__} must use membership or a literal: {line.strip()}"

    assert ACP_BACKENDS_INTERNAL_SANDBOX == frozenset({ACP_BACKEND_KIRO}), (
        "only kiro-cli ships an internal OS sandbox; adding a member here waives "
        "Kiro Crew's own seatbelt for that harness on macOS"
    )


def test_auto_model_is_opt_in() -> None:
    """H6: serving the ``"auto"`` model id is membership, not a non-kiro test.

    ``"auto"`` is a kiro-namespace id rather than a protocol concept: the
    kiro-agent family advertises it as a row of its own model list, and a spec
    adapter rejects it at the wire. Deciding by "is the backend id non-empty"
    reads as a kiro test only because ``ACP_BACKEND_KIRO`` is ``""`` — it is the
    forbidden negative form, and it stripped the Auto row from KAS, which speaks
    kiro's dialect and does serve the id.
    """
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_AUTO_MODEL
    assert ACP_BACKEND_KAS in ACP_BACKENDS_AUTO_MODEL
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_AUTO_MODEL
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_AUTO_MODEL

    # The gateway STATES the capability rather than leaving the dashboard to infer
    # it from the id, so the membership has exactly one home. Read from the set at
    # both response sites: the success envelope and the degraded 503, which is the
    # steady state of an adapter with no live session and therefore precisely when
    # the picker has to decide whether it may synthesize the row.
    from kiro_crew.dashboard.handlers import agents as agents_handler

    source = inspect.getsource(agents_handler.api_models)
    assert source.count('"serves_auto": _alt_backend in ACP_BACKENDS_AUTO_MODEL') == 2


def test_kiro_credits_is_opt_in() -> None:
    """H6: billing against the operator's Kiro credit plan is membership.

    ``CAP_BILLING`` is not a usable proxy for it and must not be reused as one:
    that level says whether Kiro Crew can READ a cost signal, and
    claude-agent-acp is DEGRADED there because it reports a real cumulative
    dollar figure — from another account. Whose balance moved is a property of
    the account a harness authenticates to, not of the wire dialect, which is
    also why KAS is a member while sitting at UNVERIFIED for billing.

    Both readouts fail toward hiding a number rather than asserting a balance,
    because the alternative costs real money: populating the pill spends a BILLED
    ``kiro-cli chat ... /usage`` turn on a 30s timer.
    """
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_KIRO_CREDITS
    assert ACP_BACKEND_KAS in ACP_BACKENDS_KIRO_CREDITS
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_KIRO_CREDITS
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_KIRO_CREDITS

    # The gateway STATES it, at both consumers, so no dashboard carries a second
    # copy of the set: the status payload's harness block and the usage endpoint
    # that would otherwise spend credits scraping a balance nobody is drawing.
    from kiro_crew.dashboard import state as dashboard_state
    from kiro_crew.dashboard.handlers import sessions as sessions_handler

    assert "bills_kiro_credits" in inspect.getsource(dashboard_state._harness_status)
    assert "bills_kiro_credits" in inspect.getsource(sessions_handler.api_sessions_usage)


def test_capability_sets_are_subsets_of_known_backends() -> None:
    """H8: a capability cannot be granted to an identifier nothing recognizes.

    A member that is not in ``ACP_BACKENDS_KNOWN`` is dead config at best and a
    typo that silently grants nothing at worst.

    Discovered from the module rather than listed here, because a hand-kept list
    fails in the direction that matters: a set added to ``acp/types.py`` and
    forgotten here is exactly the one whose members nobody has checked.
    """
    sets = {
        name: value
        for name, value in vars(acp_types).items()
        if name.startswith("ACP_BACKENDS_")
        and name != "ACP_BACKENDS_KNOWN"
        and isinstance(value, frozenset)
    }
    # A rename that empties this dict would pass every assertion below.
    assert len(sets) >= 5, f"capability sets are no longer discoverable: {sorted(sets)}"
    # The selectable set is a REGISTRY, not a module frozenset, so the scan above
    # cannot reach it. Checked alongside the capability sets because the same
    # invariant applies: ``register_selectable_backend`` already refuses an
    # unknown id, and this is the belt to that braces — a member arriving some
    # other way still has to be a backend the code recognizes.
    sets["selectable_backends()"] = frozenset(selectable_backends())
    for name, members in sets.items():
        assert members <= ACP_BACKENDS_KNOWN, f"{name} names an unknown backend"


def test_unknown_backend_rejected_at_construction() -> None:
    """H8: an unrecognized harness id is refused, not silently spawned as Kiro.

    ``ACP_BACKEND_KIRO`` is the empty string, so a value that falls through every
    identity check spawns kiro-cli under a foreign label. Construction is where
    that has to stop.
    """
    with pytest.raises(ValueError, match="acp_backend"):
        providers_acp.AcpProvider(acp_backend="byo-harness")


# ---------------------------------------------------------------------------
# Group C: the Kiro path keeps its own machinery
# ---------------------------------------------------------------------------


def test_kiro_spawn_argv_keeps_its_own_branch() -> None:
    """H9: the Kiro spawn keeps agent materialization and the model pin.

    kiro-cli discovers selectable modes from ``~/.kiro/agents/*.json`` at
    startup, so a missing agent file makes a later ``set_mode`` fail with "Mode
    not found"; and ``--model`` at spawn is the only way to run a model outside
    the agent's own provider. A refactor that treats Kiro as one entry among N
    drops both without failing anything else.

    The Kiro spawn now lives in its own harness rather than as a branch inside
    the runtime, which is what keeps this invariant satisfiable at all: the
    materialization and the model pin are in a file no other host shares, so a
    host added later cannot reach them and cannot generalize them away.
    """
    source = inspect.getsource(KiroHarness.resolve_spawn)
    assert "ensure_agent_materialized" in source
    assert '"--model"' in source
    assert '"--agent"' in source


def test_handshake_is_per_backend() -> None:
    """H10: no lowest-common-denominator handshake.

    Collapsing the two capability dicts into one every harness accepts silently
    downgrades what the Kiro session declares.

    Each harness answers with its OWN constant, so the two answers cannot be
    merged without deleting one of these two lines. The protocol version is
    pinned alongside because the hosts disagree on its TYPE, and a shared
    handshake would have to pick one and break the other outright.
    """
    kiro_source = inspect.getsource(KiroHarness.client_capabilities.fget)
    kas_source = inspect.getsource(KasHarness.client_capabilities.fget)
    assert "ACP_CLIENT_CAPABILITIES" in kiro_source
    assert "KAS_CLIENT_CAPABILITIES" in kas_source
    assert KAS_CLIENT_CAPABILITIES != ACP_CLIENT_CAPABILITIES
    assert KiroHarness().protocol_version != KasHarness().protocol_version


def test_every_known_backend_has_a_label() -> None:
    """H11: the provider label is a closed mapping and Kiro is its default.

    The label indexes resume compatibility, session-map persistence, and
    session-file cleanup routing. A harness with no label of its own persists as
    a Kiro session, and the map then prunes its id for want of a Kiro transcript.

    Reads the REAL mapping rather than restating it. This test used to keep its own
    copy of the dict, which silently went stale the moment a backend was added —
    it was missing codex and still passing its own assertion, because the copy was
    self-consistent. Kiro is deliberately absent from the table: its label IS the
    default, so it is added here rather than stored.
    """
    labels = {ACP_BACKEND_KIRO: PROVIDER_LABEL_DEFAULT, **PROVIDER_LABELS_BY_BACKEND}
    assert set(labels) == set(ACP_BACKENDS_KNOWN), (
        "a known backend has no PROVIDER_LABEL_* of its own, so it would persist "
        "under the kiro label — add one in acp/types.py and an entry in "
        "PROVIDER_LABELS_BY_BACKEND"
    )
    assert len(set(labels.values())) == len(labels), "two backends share a label"


def test_opencode_is_selectable_and_answerable() -> None:
    """H1/H8: offered only because the build can answer for it, on two counts.

    Asserted TOGETHER, like the codex pairing below, because either half alone is
    the state the pairing exists to prevent. Without the install probe a failed
    session arrives with nothing to act on; without ENFORCED routing the switch
    offers a harness whose tool calls would not reach the host gate -- and this
    harness's own permission default is permissive, so that second half is not
    hypothetical.
    """
    from kiro_crew.agent_sdk import tool_gate
    from kiro_crew.agent_sdk.backend_install import _PROBES

    assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_KNOWN
    assert ACP_BACKEND_OPENCODE in BASELINE_SELECTABLE_BACKENDS
    assert ACP_BACKEND_OPENCODE in selectable_backends()
    assert ACP_BACKEND_OPENCODE in _PROBES, (
        "opencode is offered in the switch, so backend_install must be able to say "
        "what is missing when a session fails to start"
    )
    assert tool_gate.is_enforced(ACP_BACKEND_OPENCODE), (
        "opencode is offered in the switch, so its routing must be one this core "
        "enforces -- its own permission default asks for nothing"
    )


def test_codex_is_selectable_and_answerable() -> None:
    """H1/H8: a harness may only be offered once the build can answer for it.

    Codex was withheld for one stated reason -- ``backend_install.py`` had no probe,
    so its install row could only read ``unknown`` and a failed session arrived with
    nothing to act on. The probe closes that, which is what makes the switch
    honest rather than merely present.

    Asserted TOGETHER on purpose: selectability without a probe is the exact state
    the withholding existed to prevent, so a future change that removed the probe
    while leaving the baseline entry would fail here rather than silently ship a
    switch with nothing behind it.
    """
    from kiro_crew.agent_sdk.backend_install import _PROBES

    assert ACP_BACKEND_CODEX in ACP_BACKENDS_KNOWN
    assert ACP_BACKEND_CODEX in BASELINE_SELECTABLE_BACKENDS
    assert ACP_BACKEND_CODEX in selectable_backends()
    assert ACP_BACKEND_CODEX in _PROBES, (
        "codex is offered in the switch, so backend_install must be able to say "
        "what is missing when a session fails to start"
    )


def test_codex_tool_calls_are_gated_before_it_is_offered() -> None:
    """A selectable harness must route its tool calls, or the switch is a trap.

    This is the invariant that makes admission mean something: the picker offering
    an id and the gate being armed for it are separate facts, and selectability
    without routing would put the operator's narrowing silently out of circuit.
    """
    from kiro_crew import acp_tool_gate

    verdict, _reason = acp_tool_gate.routing_verdict(ACP_BACKEND_CODEX)
    assert verdict is acp_tool_gate.Verdict.ROUTED
    assert acp_tool_gate.is_enforced(ACP_BACKEND_CODEX) is True
    assert acp_tool_gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX), (
        "ACP v1 cannot require a prompt for a passive read, so the credential "
        "homes must be denied at the OS boundary instead"
    )


def test_codex_carries_its_own_provider_label() -> None:
    """H11: the label is what keeps a codex session out of the kiro namespace.

    Resume compatibility, session-map persistence and session-file cleanup all index
    this key, so a codex session labelled ``acp`` would be resumed as kiro and then
    pruned for want of a kiro transcript.
    """
    client = MagicMock()
    client.backend = ACP_BACKEND_CODEX
    provider = MagicMock(spec=providers_acp.AcpProvider)
    provider.client = client
    # Set on the PROVIDER, not only the client: provider_label resolves identity
    # from the provider's own backend string and never reaches through to the live
    # client (H14), so a client-only id would leave this reading the mock.
    provider.backend = ACP_BACKEND_CODEX
    assert providers_acp.provider_label(provider) == PROVIDER_LABEL_CODEX
    assert PROVIDER_LABEL_CODEX != PROVIDER_LABEL_DEFAULT


def test_model_switch_channel_is_opt_in() -> None:
    """H6: the config-option model channel is granted by membership, not negation.

    kiro-cli switches models with ``session/set_model``; the claude and codex
    adapters implement no such request and expose the model as a session config
    option instead. Read as ``not is_kiro`` this would hand the config-option path
    to every harness added later, and a harness that implements neither would
    silently no-op its model switch.
    """
    assert ACP_BACKEND_CLAUDE in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert ACP_BACKEND_KIRO not in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION <= ACP_BACKENDS_KNOWN
    source = "\n".join(
        (
            inspect.getsource(acp_client.AcpClient.set_model),
            inspect.getsource(acp_client.AcpClient._apply_startup_model),
        )
    )
    assert (
        "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION" in source
    ), "the model switch must read the membership set, not a per-backend literal"


def test_effort_channel_is_opt_in() -> None:
    """H6: the effort channel is granted by membership, not by "not claude".

    The two channels are separate opt-ins because a harness can have neither. Read
    as ``not is_claude_backend``, an adapter harness is handed kiro's ``/effort``
    slash command, which rides ``_kiro.dev/commands/execute`` — a verb it does not
    implement — so the push fails -32601 and the dashboard resets the session.
    """
    assert ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION <= ACP_BACKENDS_KNOWN
    assert ACP_BACKENDS_KIRO_SLASH_COMMANDS <= ACP_BACKENDS_KNOWN
    # Disjoint: a harness must not be told to push effort down both channels.
    assert not (ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION & ACP_BACKENDS_KIRO_SLASH_COMMANDS)
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_KIRO_SLASH_COMMANDS
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_KIRO_SLASH_COMMANDS
    source = "\n".join(
        (
            inspect.getsource(providers_acp.AcpProvider.change_effort),
            inspect.getsource(providers_acp.AcpProvider.clear_effort),
            inspect.getsource(providers_acp.AcpProvider._apply_effort_overlay),
            inspect.getsource(providers_acp.AcpProvider._apply_tool_search_overlay),
            inspect.getsource(providers_acp.AcpProvider.stream_command),
        )
    )
    assert "is_claude_backend" not in source, (
        "the effort, overlay and slash-command seams must read a membership set; "
        "a claude test here decides the path for every harness added later"
    )


def test_only_overlay_readers_are_written_to() -> None:
    """H6: the cli.json overlay is written only for the harnesses that read it.

    The clear side (``_clear_cli_overlay_effort``) is membership-gated, so a write
    gated on anything wider leaves a stale overlay in the user's workspace that no
    later clear can reach — and the overlay names an effort level, so a harness
    that DOES read the file later inherits a level nobody set for it.
    """
    for fn in (
        providers_acp.AcpProvider._apply_effort_overlay,
        providers_acp.AcpProvider._apply_tool_search_overlay,
    ):
        source = inspect.getsource(fn)
        assert (
            "ACP_BACKENDS_KIRO_SLASH_COMMANDS" in source
        ), f"{fn.__name__}: overlay write is not scoped to the overlay's readers"


def test_codex_spawn_keeps_its_own_branch() -> None:
    """H9/H10: codex resolves its own adapter and declares its own handshake.

    Falling through to the kiro branch would spawn kiro-cli under a codex label —
    the exact failure ACP_BACKENDS_KNOWN's rejection exists to prevent one step
    earlier — and folding its protocol version into the claude literal would make a
    future divergence a silent downgrade for whichever harness moved first.

    Codex is an upstream-owned harness, so its spawn resolves through upstream's
    in-client ``_resolve_codex_acp_bin`` (operator rule: where upstream ships a
    harness, this build runs upstream's implementation of it entirely; the fork
    keeps only the adapters upstream does not have). The fork's own
    ``acp/codex.py`` resolver, which added a Node-major floor, is retired with
    that rule. Not relaxed to a bare ``"codex" in spawn_source``, which a comment
    would satisfy.
    """
    spawn_source = inspect.getsource(acp_client.AcpClient._spawn)
    assert "_is_codex" in spawn_source
    assert "_resolve_codex_acp_bin" in spawn_source
    assert acp_client.PROTOCOL_VERSION_CODEX is not None
    # The per-backend wire contract lives in the module-level
    # ``_wire_contract_for_backend``, which reads ONE table
    # (``_PROTOCOL_VERSION_BY_BACKEND``). Both halves are pinned so the
    # extraction cannot decay: codex's OWN literal is the table's entry (H10),
    # and the contract helper still reaches that table instead of carrying a
    # second copy that could drift (H13).
    table = acp_client._PROTOCOL_VERSION_BY_BACKEND
    assert table[ACP_BACKEND_CODEX] is acp_client.PROTOCOL_VERSION_CODEX
    contract_source = inspect.getsource(acp_client._wire_contract_for_backend)
    assert "_PROTOCOL_VERSION_BY_BACKEND" in contract_source
    assert "_wire_contract_for_backend" in inspect.getsource(
        acp_client.AcpClient._initialize_session
    )


def test_each_mcp_seam_is_spliced_only_for_its_own_harness() -> None:
    """H6/mcp-injection-composition: the wire array is ONE gated call, never a
    per-harness splice.

    Upstream splices `_claude_session_mcp_servers` / `_codex_session_mcp_servers`
    / `_opencode_session_mcp_servers` directly and ungated (each guarded only by
    its own `self._is_<harness>` check, so a hand-written override of two of them
    could still cross-contaminate a third's session). This build routes every
    non-kiro backend through ONE tool-gate-gated call instead
    (`_gated_session_mcp_servers`, `.fork-sync.yml`, divergences/
    mcp-injection-composition): a harness whose permission route is unproven gets
    NO Crew control plane, so there is no ungated per-harness hook left to
    cross-splice in the first place. Pinned at the source, in the file's existing
    idiom, because the splice sits inside an async session-setup path with no
    unit-level seam.
    """
    for fn in (
        acp_client.AcpClient._new_session_following_substitution,
        acp_client.AcpClient._initialize_session,
    ):
        source = inspect.getsource(fn)
        if "_gated_session_mcp_servers" not in source:
            continue
        # The wire site is one call, not an array literal: `self._is_claude`
        # and friends legitimately appear elsewhere in the same function (the
        # `_meta.claudeCode` block right after it), which is unrelated to how
        # the array is spliced. Scope to that one call expression.
        start = source.index('"mcpServers": ')
        line = source[start : source.index("\n", start)]
        assert "if self._is_codex" not in line, f"{fn.__name__}: codex seam spliced ungated"
        assert "if self._is_claude" not in line, f"{fn.__name__}: claude seam spliced ungated"
        assert "if self._is_opencode" not in line, f"{fn.__name__}: opencode seam spliced ungated"
        assert (
            "await self._gated_session_mcp_servers()" in line
        ), f"{fn.__name__}: wire array is no longer the one gated call"


def test_codex_mcp_seam_projects_through_its_mirror() -> None:
    """The seam is FILLED, and it fills from the mirror rather than from itself.

    An empty array is byte-identical for kiro-cli (``--agent`` carries its
    servers) and a real gap for codex: the adapter reads no spec of Crew's, so a
    selectable public backend would serve sessions with no ``spawn_run``, no
    ``cron_add``, no ``send_message`` and no error anywhere.

    What this pins is WHERE the array comes from. A translator written here rather
    than in ``providers/mirrors/codex.py`` is the shape the mirror folder exists to
    stop: one per-harness override per author, each rediscovering the same
    projection.
    """
    source = inspect.getsource(acp_client.AcpClient._codex_session_mcp_servers)
    assert "self._session_mcp_servers()" in source
    assert acp_backends.ACP_BACKEND_CODEX in acp_backends.ACP_BACKENDS_SESSION_MCP_ARRAY
    assert mirrors.mirror_for(acp_backends.ACP_BACKEND_CODEX) is not None


def test_opencode_mcp_seam_projects_through_its_mirror() -> None:
    """The same three facts for opencode, and the one that had to be MEASURED.

    This seam was empty on the reading that opencode's ``initialize`` advertises
    ``mcpCapabilities`` of http and sse and no stdio, so the array could not carry
    Crew's stdio servers. ACP's ``McpCapabilities`` has exactly two boolean fields
    and no stdio field, so no conforming agent can advertise stdio and the absence
    was never evidence -- driven against a real ``opencode acp``, the element Crew
    already emits is accepted. Until then a selectable harness served sessions with
    no ``spawn_run``, no ``cron_add`` and no ``send_message``, and nothing was red.

    What it must NOT contain is codex's transport filter: this harness accepts
    ``http`` and ``sse`` elements too, so dropping them would remove capability the
    session would have had.
    """
    source = inspect.getsource(acp_client.AcpClient._opencode_session_mcp_servers)
    assert "self._session_mcp_servers()" in source
    assert acp_backends.ACP_BACKEND_OPENCODE in acp_backends.ACP_BACKENDS_SESSION_MCP_ARRAY
    assert mirrors.mirror_for(acp_backends.ACP_BACKEND_OPENCODE) is not None
    # The absent filter is read off the AST rather than the text, because the
    # docstring EXPLAINS why codex's filter is not applied here -- a substring check
    # would be satisfied by deleting that explanation and broken by writing it.
    fn = ast.parse(textwrap.dedent(source)).body[0]
    assert isinstance(fn, ast.FunctionDef)
    called = {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "drop_unadvertised_transports" not in called, (
        "the opencode hook applies codex's transport filter, which would drop the "
        "http and sse elements this harness accepts"
    )


def test_model_preflight_allows_unknown_advertised_set() -> None:
    """H12: an empty or unknown advertised set means allow.

    Harnesses advertise model ids in their own spelling. A membership test that
    treats "not in this list" as unusable withholds every legitimate model the
    moment a second namespace exists.
    """
    assert acp_client.model_is_unusable("anything", set()) is False
    assert acp_client.model_is_unusable("anything", None) is False
    assert acp_client.model_is_unusable("absent", {"present"}) is True


def test_model_namespace_membership() -> None:
    """H12: the pre-flight namespace set names exactly the backends whose
    advertised ids are the ids their own ``session/set_model`` accepts.

    The claude seam must NOT be a member: it advertises bare ids
    (``claude-opus-4-8[1m]``) while the configured model is the prefixed
    provider id, so a membership test across those namespaces withholds every
    legitimate model. copilot MUST be a member — its advertised
    ``provider/model`` ids are exactly what its ``session/set_model`` accepts,
    so a config carrying a kiro-namespace id (e.g.
    ``agents.default.model = claude-opus-4.6``) must be withheld at startup
    rather than sent and rejected by the backend. opencode is upstream's
    harness and follows upstream's model handling, which runs no entitlement
    pre-flight for it, so it is deliberately NOT a member (operator rule: an
    upstream-owned harness runs upstream's implementation entirely).
    """
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_MODEL_NAMESPACE
    assert ACP_BACKEND_OPENCODE not in ACP_BACKENDS_MODEL_NAMESPACE
    for backend in (
        ACP_BACKEND_KIRO,
        ACP_BACKEND_KAS,
        ACP_BACKEND_COPILOT,
    ):
        assert backend in ACP_BACKENDS_MODEL_NAMESPACE


# ---------------------------------------------------------------------------
# The added-line gate
# ---------------------------------------------------------------------------


def test_added_line_gate_self_test_passes() -> None:
    """H5: the diff-scoped gate still detects every shape it claims to.

    A gate that has silently stopped matching reads as a green signal, which is
    worse than no gate. CI runs this same self-test before the real check; this
    test makes a local ``pytest`` run catch a broken rule too.
    """
    result = subprocess.run(
        [sys.executable, _GATE_PATH, "--test"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_added_line_gate_reports_without_enforcing() -> None:
    """H5: with no base ref the gate reports and exits 0.

    The tree carries pre-existing negative tests in the dormant claude seam.
    Enforcing whole-tree would fail every PR until those are converted and charge
    the break to whoever pushed next, so the backlog is a report.
    """
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_BASE_REF"}
    result = subprocess.run(
        [sys.executable, _GATE_PATH],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "harness gate" in result.stdout


def test_added_line_gate_flags_a_planted_negative_test(tmp_path, monkeypatch) -> None:
    """H5: a violation in an explicitly-scanned file exits 1.

    Covers the exit-code contract the script's own ``--test`` mode cannot reach,
    since that mode only exercises the rule engine. The probe is planted in a
    temp tree with ``REPO_ROOT`` repointed at it — writing into the real
    ``src/`` would leave a stray module behind for every later test in the
    session if this one failed mid-way.
    """
    spec = importlib.util.spec_from_file_location("check_harness_parity", _GATE_PATH)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    sys.modules["check_harness_parity"] = gate
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))

    planted = "probe_harness.py"
    (tmp_path / planted).write_text(
        "def eligible(self):\n    return not self.is_claude_backend\n",
        encoding="utf-8",
    )
    assert gate.main([planted]) == 1

    (tmp_path / planted).write_text(
        "def eligible(self):\n    return self.is_kiro_backend\n",
        encoding="utf-8",
    )
    assert gate.main([planted]) == 0


# ---------------------------------------------------------------------------
# H6 completeness — every per-host answer on the runtime path reads its table
# ---------------------------------------------------------------------------
#
# H5 and H6 each catch a SITE: the added-line gate catches a negative identity
# test, and the assertions above catch one capability granted by negation. Neither
# catches the shape below, which is the one that reaches a user: a site that answers
# a per-host question correctly for the hosts it was written against,
# from an identity test or from "is this host on the shared runtime", while a
# membership table already holds the answer. Every one of them read correctly with
# kiro and KAS and answered wrongly for the third host the moment one arrived --
# steer advertised then met with ``-32601``, a configured effort silently dropped,
# an empty model picker, an entitlement probe that could not heal the snapshot it
# exists for.
#
# So this section is a COMPLETENESS gate rather than another per-site pin. It has
# two halves, and the second is what makes it a gate:
#
#   1. Every answer below is asserted equal to its table FOR EVERY BACKEND IN
#      ``ACP_BACKENDS_KNOWN``, not only for the ones the site was written against.
#      A host that is not on the runtime today still goes through the answer, so a
#      divergence is caught before that host is ever admitted.
#   2. Every backend-identity comparison in the runtime-path modules must be
#      DECLARED below with a reason. A new one goes red until its author either
#      points it at a table or records why identity is the honest answer there.
#
# Half 2 is the part a future author meets. It cannot decide whether a reason is
# good -- a reviewer does that -- but it makes adding a seventh site a deliberate
# act with a written justification instead of a line nobody notices.

_RUNTIME_PATH_MODULES = (
    "src/kiro_crew/acp/runtime.py",
    "src/kiro_crew/acp/session_handle.py",
)

#: Backend-identity comparisons the runtime path is allowed to make, keyed by
#: ``(module, enclosing function)``, with why a table cannot answer instead. A
#: comparison against an ``ACP_BACKEND_*`` constant that is not listed here fails
#: :func:`test_every_runtime_path_identity_test_is_declared`.
_DECLARED_IDENTITY_TESTS: dict[tuple[str, str], str] = {
    (
        "src/kiro_crew/acp/runtime.py",
        "create_session",
    ): "KAS alone is handed no ``--agent`` and its custom agents omit ``mcpServers``, "
    "so session injection is the single owner of its Crew servers "
    "(``_with_kas_managed_servers``). No capability set answers 'this host needs "
    "Crew's managed servers merged in': membership would have to mean the OPPOSITE "
    "for kiro, which reads them off disk, so a set here would have to be spelled as "
    "an absence. Pinned by divergences/mcp-injection-composition.",
    (
        "src/kiro_crew/acp/runtime.py",
        "load_session",
    ): "KAS alone must have its custom agents re-attached on resume, and no harness "
    "property means 'this host needs its agent re-sent'. Adding one would cost the "
    "kiro path an awaited step it does not need (H13).",
    (
        "src/kiro_crew/acp/session_handle.py",
        "stream_command",
    ): "``_kiro.dev/commands/execute`` is kiro-cli's own RPC. The positive test is what "
    "makes every other harness fail CLOSED onto the prompt transport instead of "
    "inheriting a kiro-only verb (H5/H6).",
    (
        "src/kiro_crew/acp/session_handle.py",
        "set_model",
    ): "KAS is deliberately absent from ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION``: it "
    "advertises the option and accepts the resolved id, so the member ladder would only "
    "add retries after a terminal failure and would turn a refusal into a silent "
    "stay-on-default. The reason is recorded at the branch.",
    (
        "src/kiro_crew/acp/session_handle.py",
        "ensure_served_default",
    ): "The served-default backfill reads kiro-cli's own ``currentModelId`` semantics. "
    "Another host reaching it would have its resolved id rewritten from a list it did "
    "not author.",
    (
        "src/kiro_crew/acp/session_handle.py",
        "_handle_update",
    ): "KAS emits its own notification discriminants. The positive gate restores those "
    "displays without touching the kiro parser, and returns None for anything not "
    "KAS-specific so shared frames still fall through (H5).",
}


def _handle_for(backend: str):
    """A session handle whose runtime names *backend* — enough for every answer here."""
    import asyncio

    rt = MagicMock()
    rt.acp_backend = backend
    return acp_runtime.AcpSessionHandle("s-parity", asyncio.Queue(), rt)


def _runtime_for(backend: str):
    return acp_runtime.AcpRuntime(work_dir="/tmp", acp_backend=backend)


#: A ``model`` select and nothing else — the shape a host advertises when it has no
#: ``models`` object, which is what the advertised-selection table decides about.
_SELECT_ONLY_SESSION_RESP = {
    "configOptions": [{"id": "model", "type": "select", "options": [{"value": "some-model"}]}]
}


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_steer_advertisement_matches_the_steer_table(backend):
    """H6: the handle advertises steer iff the host is in ``ACP_BACKENDS_STEER``.

    Ran for every known backend, which is the point: this answer was a literal
    ``True`` for years and was honest only while ``AcpRuntime`` served one host.
    """
    assert _handle_for(backend).supports_steer is (backend in ACP_BACKENDS_STEER)


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_agent_activation_by_mode_matches_the_routing_table(backend):
    """H6: ``session/set_mode`` names a Crew agent iff the host is governed by a spec.

    The hosts an agent spec governs are exactly the hosts that HAVE an agent to
    activate, so the routing table already answers this and the runtime must not
    re-derive it from "is this kiro".
    """
    from kiro_crew import acp_tool_gate

    expected = acp_tool_gate.routing_for(backend) is acp_tool_gate.Routing.AGENT_SPEC
    assert _runtime_for(backend)._activates_agent_by_mode() is expected


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_the_model_select_fold_matches_the_advertised_selection_table(backend):
    """H6: a ``model`` select becomes the advertised list iff the host opted in.

    Both readers go through one fold, so asserting the fold covers the session-init
    capture and the entitlement probe together.
    """
    from kiro_crew.acp.session_handle import (
        advertised_models_from_session,
        models_from_config_options,
    )

    opted_in = backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION
    assert (models_from_config_options(_SELECT_ONLY_SESSION_RESP, backend) is not None) is opted_in
    folded = advertised_models_from_session(_SELECT_ONLY_SESSION_RESP, backend)
    assert bool(folded) is opted_in


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_unprojected_pooled_mcp_is_refused_for_exactly_the_mirrored_hosts(backend):
    """H6: pooled servers are refused iff this host's MCP surface needs a projection.

    Read from the mirror registry rather than from a backend name, so a host added to
    ``providers.mirrors`` inherits the refusal instead of the gap.
    """
    from kiro_crew.acp.client import AcpToolGateUnroutable

    rt = _runtime_for(backend)
    rt._refuse_unprojected_pooled_servers([])  # empty never refuses, for any host
    pooled = [{"name": "brokered", "command": "x"}]
    if mirrors.has_mirror(backend):
        with pytest.raises(AcpToolGateUnroutable):
            rt._refuse_unprojected_pooled_servers(pooled)
    else:
        rt._refuse_unprojected_pooled_servers(pooled)


def test_every_runtime_path_identity_test_is_declared():
    """H6 completeness: a NEW backend-identity test on the runtime path goes red here.

    The equality tests above pin the answers that exist. This pins the SET of sites
    allowed to answer from identity at all, so one more cannot arrive unnoticed --
    which is how every site in the declaration list below arrived.
    """
    import ast

    undeclared: list[str] = []
    for rel in _RUNTIME_PATH_MODULES:
        path = os.path.join(_REPO_ROOT, rel)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        scope: list[str] = []

        def visit(node, scope=scope, rel=rel):
            pushed = False
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope.append(node.name)
                pushed = True
            if isinstance(node, ast.Compare):
                for cmp_node in [node.left, *node.comparators]:
                    name = getattr(cmp_node, "id", None)
                    if not (isinstance(name, str) and name.startswith("ACP_BACKEND_")):
                        continue
                    if name.startswith("ACP_BACKENDS_"):
                        continue  # a set membership test IS the sanctioned form
                    where = scope[-1] if scope else "<module>"
                    if (rel, where) not in _DECLARED_IDENTITY_TESTS:
                        undeclared.append(f"{rel}:{node.lineno} in {where}() compares {name}")
            for child in ast.iter_child_nodes(node):
                visit(child)
            if pushed:
                scope.pop()

        visit(tree)

    assert not undeclared, (
        "backend-identity test(s) on the runtime path with no entry in "
        "_DECLARED_IDENTITY_TESTS. Point the site at the membership table that already "
        "answers it, or add an entry saying why identity is the honest answer there:\n  "
        + "\n  ".join(undeclared)
    )


def test_the_identity_test_declarations_are_all_still_live():
    """A declaration whose site is gone must be pruned, so the list cannot rot.

    Without this the allowlist only ever grows, and a stale entry silently
    pre-approves a future site that happens to land in the same function.
    """
    import ast

    seen: set[tuple[str, str]] = set()
    for rel in _RUNTIME_PATH_MODULES:
        path = os.path.join(_REPO_ROOT, rel)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        scope: list[str] = []

        def visit(node, scope=scope, rel=rel):
            pushed = False
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope.append(node.name)
                pushed = True
            if isinstance(node, ast.Compare):
                for cmp_node in [node.left, *node.comparators]:
                    name = getattr(cmp_node, "id", None)
                    if (
                        isinstance(name, str)
                        and name.startswith("ACP_BACKEND_")
                        and not name.startswith("ACP_BACKENDS_")
                        and scope
                    ):
                        seen.add((rel, scope[-1]))
            for child in ast.iter_child_nodes(node):
                visit(child)
            if pushed:
                scope.pop()

        visit(tree)

    stale = sorted(set(_DECLARED_IDENTITY_TESTS) - seen)
    assert not stale, f"prune these dead _DECLARED_IDENTITY_TESTS entries: {stale}"
