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

import importlib.util
import inspect
import os
import subprocess
import sys
from dataclasses import fields

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp import types as acp_types
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
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABELS_BY_BACKEND,
)
from kiro_crew.acp_backends import BASELINE_SELECTABLE_BACKENDS, selectable_backends
from kiro_crew.config.loader import AgentConfig, _normalize_acp_backend
from kiro_crew.providers import acp as providers_acp

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
    """H1: discovery does not advertise an adapter before basic validation.

    Claude, Codex, goose, pi, and registry-only adapters remain described so
    their integration work can continue. Claude is withheld until its settings
    cleanup can preserve an operator's pre-existing project file. Codex is
    withheld because its read-only mode does not permission-route passive
    reads, so the standard sandbox cannot enforce the sensitive-path floor for
    that backend.

    FORK DIVERGENCE: upstream's staged admission withholds everything but
    kiro/kas. This fork additionally admits ``ACP_BACKEND_COPILOT`` and
    ``ACP_BACKEND_OPENCODE`` — unlike claude, they are not dormant/companion-only
    seams: the fork's core resolves and spawns ``copilot --acp`` / ``opencode acp``
    directly and both have been driven end to end (see ``acp/types.py``'s own
    comment above ``ACP_BACKENDS_SELECTABLE`` for the per-backend verification
    notes on the still-withheld set).
    """
    assert selectable_backends() == frozenset(
        {
            ACP_BACKEND_KIRO,
            ACP_BACKEND_KAS,
            ACP_BACKEND_COPILOT,
            ACP_BACKEND_OPENCODE,
        }
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

    ``claude`` is in the list on purpose: it is a KNOWN id that this build does not
    register, and the one gate reads the registry, so it degrades like any other
    unselectable value. Registering it is what makes it survive.
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
    """
    assert _normalize_acp_backend(ACP_BACKEND_CLAUDE) == ACP_BACKEND_KIRO
    before = set(acp_backends._selectable)
    try:
        acp_backends.register_selectable_backend(ACP_BACKEND_CLAUDE)
        assert _normalize_acp_backend(ACP_BACKEND_CLAUDE) == ACP_BACKEND_CLAUDE
    finally:
        acp_backends._selectable.clear()
        acp_backends._selectable.update(before)
    assert _normalize_acp_backend(ACP_BACKEND_CLAUDE) == ACP_BACKEND_KIRO


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
    """H9: the Kiro branch keeps agent materialization and the model pin.

    kiro-cli discovers selectable modes from ``~/.kiro/agents/*.json`` at
    startup, so a missing agent file makes a later ``set_mode`` fail with "Mode
    not found"; and ``--model`` at spawn is the only way to run a model outside
    the agent's own provider. A dict-of-builders refactor that treats Kiro as one
    entry among N drops both without failing anything else.
    """
    source = inspect.getsource(acp_runtime.AcpRuntime._resolve_spawn_argv)
    assert "ensure_agent_materialized" in source
    assert '"--model"' in source
    assert '"--agent"' in source


def test_handshake_is_per_backend() -> None:
    """H10: no lowest-common-denominator handshake.

    Collapsing the two capability dicts into one every harness accepts silently
    downgrades what the Kiro session declares.
    """
    kiro_version, kiro_capabilities = acp_runtime._runtime_wire_contract(ACP_BACKEND_KIRO)
    kas_version, kas_capabilities = acp_runtime._runtime_wire_contract(ACP_BACKEND_KAS)

    assert kiro_version == acp_runtime.PROTOCOL_VERSION
    assert kas_version == acp_runtime.PROTOCOL_VERSION_KAS
    assert kiro_capabilities is ACP_CLIENT_CAPABILITIES
    assert kas_capabilities is KAS_CLIENT_CAPABILITIES
    assert KAS_CLIENT_CAPABILITIES != ACP_CLIENT_CAPABILITIES


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
    legitimate model. copilot and opencode MUST be members — their advertised
    ``provider/model`` ids are exactly what their ``session/set_model``
    accepts, so a config carrying a kiro-namespace id (e.g.
    ``agents.default.model = claude-opus-4.6``) must be withheld at startup
    rather than sent and rejected by the backend.
    """
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_MODEL_NAMESPACE
    for backend in (
        ACP_BACKEND_KIRO,
        ACP_BACKEND_KAS,
        ACP_BACKEND_COPILOT,
        ACP_BACKEND_OPENCODE,
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
