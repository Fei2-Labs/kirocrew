"""Per-agent model resolution on the acp (kiro-cli) provider factory."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import DEFAULT_MODEL, KiroCrewConfig, build_provider_factory


class TestAcpPerAgentModel:
    """A custom agent on the acp (kiro-cli) backend must run its own model.

    Regression: the acp factory previously passed model=None for custom agents,
    relying on kiro's session/set_mode to resolve the agent's model — but
    set_mode switches the prompt/tools, not the model, so the handshake skipped
    session/set_model and kiro fell back to its cli.json chat.defaultModel.
    These tests exercise the REAL factory (create_provider_factory) so the model
    actually threaded into AcpProvider is asserted end to end.
    """

    @staticmethod
    def _acp_cfg():
        # KiroCrew is KiroACP-only, so the factory is always acp; a plain
        # instance keeps construction side-effect-free (AcpProvider.__init__
        # builds an AcpClient without spawning kiro-cli).
        return KiroCrewConfig()

    def test_custom_agent_threads_its_declared_model(self):
        cfg = self._acp_cfg()
        with patch.object(KiroCrewConfig, "_resolve_named_agent_model", return_value="gpt-5.6-sol"):
            provider = cfg.create_provider_factory()(
                session_key="cron:job:run", agent="smle-triage-canary"
            )
        # The declared model must reach the client verbatim (not the global
        # default, and not the DEFAULT_MODEL "auto" sentinel).
        assert provider.client._model == "gpt-5.6-sol"
        assert provider.client._is_claude is False

    def test_model_override_wins_over_agent_model(self):
        cfg = self._acp_cfg()
        with patch.object(KiroCrewConfig, "_resolve_named_agent_model", return_value="gpt-5.6-sol"):
            provider = cfg.create_provider_factory()(
                session_key="cron:job:run",
                agent="smle-triage-canary",
                model_override="claude-sonnet-4.6",
            )
        assert provider.client._model == "claude-sonnet-4.6"

    def test_unresolved_agent_model_falls_back_to_the_global_default(self):
        # When the agent declares no model, _resolve_named_agent_model returns ""
        # and the factory falls through to the configured global default. This
        # tier used to be skipped entirely for named agents, so an agent pinning
        # nothing ignored the user's configured default and let kiro pick from
        # cli.json instead — the global was not really a global.
        cfg = self._acp_cfg()
        cfg.agent.model = "claude-sonnet-4.6"
        with patch.object(KiroCrewConfig, "_resolve_named_agent_model", return_value=""):
            provider = cfg.create_provider_factory()(
                session_key="cron:job:run", agent="no-model-agent"
            )
        assert provider.client._model == "claude-sonnet-4.6"

    def test_adapter_only_inheritance_hint_does_not_clear_the_kiro_model(self):
        """H13: adapter dispatch kwargs cannot change the direct Kiro factory."""
        cfg = self._acp_cfg()
        cfg.agent.model = "kiro-global"

        provider = cfg.create_provider_factory()(
            session_key="subagent:child",
            inherit_config_model=False,
        )

        assert provider.client._model == "kiro-global"

    def test_unresolved_agent_and_unset_global_leaves_the_backend_to_pick(self):
        # Nothing pinned anywhere: the global is the "auto" sentinel and the
        # installed agent file declares nothing either, so no session/set_model
        # is sent and kiro resolves from its own config. _resolve_agent_model is
        # patched because the unpatched call reads the HOST's real
        # ~/.kiro/agents/kirocrew.json, which pins a model on a dev machine.
        cfg = self._acp_cfg()
        cfg.agent.model = DEFAULT_MODEL
        with (
            patch.object(KiroCrewConfig, "_resolve_named_agent_model", return_value=""),
            patch.object(KiroCrewConfig, "_resolve_agent_model", return_value=DEFAULT_MODEL),
        ):
            provider = cfg.create_provider_factory()(
                session_key="cron:job:run", agent="no-model-agent"
            )
        assert provider.client._model == DEFAULT_MODEL

    def test_agent_pin_still_outranks_the_global_default(self):
        # The new global fallback must not overtake an agent that pins a model.
        cfg = self._acp_cfg()
        cfg.agent.model = "claude-sonnet-4.6"
        with patch.object(KiroCrewConfig, "_resolve_named_agent_model", return_value="gpt-5.6-sol"):
            provider = cfg.create_provider_factory()(
                session_key="cron:job:run", agent="pinned-agent"
            )
        assert provider.client._model == "gpt-5.6-sol"

    def test_kiro_agent_ignores_cc_model_sidecar(self, tmp_path):
        # The acp factory calls _resolve_named_agent_model, which returns the
        # kiro `model` slot and ignores a cc_model sidecar — so a kiro agent that
        # also carries a cc_model still runs its kiro model. Asserted on the real
        # resolver via its agents_dir seam, locking the resolver's contract.
        (tmp_path / "kiro-cc.json").write_text(
            json.dumps({"name": "kiro-cc", "model": "gpt-5.6-sol", "cc_model": "claude-opus-4.6"})
        )
        assert (
            KiroCrewConfig._resolve_named_agent_model("kiro-cc", agents_dir=tmp_path)
            == "gpt-5.6-sol"
        )


class TestKiroDialectSet:
    """``ACP_BACKENDS_KIRO_DIALECT`` may never drift from the descriptor table.

    The set is a LEAF-module copy of a fact ``acp/backends.py`` owns, and it
    exists only because ``members.py`` sits above ``kiro_crew.acp`` and cannot
    import it. A copy that drifts is worse than no copy: it decides which
    backends the direct Kiro factory will construct ``AcpProvider`` for, so a
    stale entry is exactly the H5 catch-all it was added to close.
    """

    def test_the_set_agrees_with_dialect_of_in_both_directions(self) -> None:
        from kiro_crew.acp import backends as acp_backends
        from kiro_crew.acp.types import ACP_BACKENDS_KNOWN
        from kiro_crew.acp_backends import ACP_BACKENDS_KIRO_DIALECT

        from_table = {
            b for b in ACP_BACKENDS_KNOWN if acp_backends.dialect_of(b) is acp_backends.Dialect.KIRO
        }
        assert set(ACP_BACKENDS_KIRO_DIALECT) == from_table, (
            "ACP_BACKENDS_KIRO_DIALECT no longer matches acp/backends.py's "
            "descriptor table; the table is the authority, so update the set"
        )

    def test_the_set_is_a_subset_of_known_backends(self) -> None:
        """H8: every capability set is a subset of ACP_BACKENDS_KNOWN."""
        from kiro_crew.acp.types import ACP_BACKENDS_KNOWN
        from kiro_crew.acp_backends import ACP_BACKENDS_KIRO_DIALECT

        assert set(ACP_BACKENDS_KIRO_DIALECT) <= set(ACP_BACKENDS_KNOWN)

    def test_kiro_itself_is_a_member(self) -> None:
        """The floor. Kiro's own id must never be routed to the dispatcher."""
        from kiro_crew.acp_backends import ACP_BACKEND_KIRO, ACP_BACKENDS_KIRO_DIALECT

        assert ACP_BACKEND_KIRO in ACP_BACKENDS_KIRO_DIALECT


class TestAcpBackendOverride:
    """Dedicated children pin the live parent harness on the factory call."""

    def test_override_wins_over_the_factory_snapshot(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_GOOSE

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_CODEX
        provider = build_provider_factory(cfg)(
            session_key="subagent:child", acp_backend=ACP_BACKEND_GOOSE
        )
        assert provider.client.backend == ACP_BACKEND_GOOSE

    def test_omitted_override_keeps_the_snapshot(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_GOOSE

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_GOOSE
        provider = build_provider_factory(cfg)(session_key="subagent:child")
        assert provider.client.backend == ACP_BACKEND_GOOSE

    def test_empty_override_pins_kiro(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_GOOSE, ACP_BACKEND_KIRO

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_GOOSE
        provider = build_provider_factory(cfg)(
            session_key="subagent:child", acp_backend=ACP_BACKEND_KIRO
        )
        assert provider.client.backend == ACP_BACKEND_KIRO

    def test_kiro_factory_serves_a_kiro_dialect_override_itself(self) -> None:
        """The direct factory honours a pick it can actually construct.

        A Kiro-snapshot config gets the DIRECT factory from
        ``create_factory`` (H13), and KAS is Kiro-dialect, so ``AcpProvider``
        is the correct provider class for it. Honouring the pick here is not a
        second check: the factory body is one
        ``members.select_provider_backend`` call with no branching of its own.
        """
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        cfg = KiroCrewConfig()

        provider = build_provider_factory(cfg)(
            session_key="subagent:child",
            acp_backend=ACP_BACKEND_KAS,
        )

        assert provider.client.backend == ACP_BACKEND_KAS
        # The Kiro provider class, not the spec adapter -- KAS speaks the Kiro
        # dialect, so this is the closed mapping's correct arm.
        from kiro_crew.providers.acp import AcpProvider, SpecAdapterAcpProvider

        assert isinstance(provider, AcpProvider)
        assert not isinstance(provider, SpecAdapterAcpProvider)

    @pytest.mark.parametrize("adapter", ["copilot", "opencode"])
    def test_kiro_factory_refuses_a_spec_dialect_override(self, adapter: str) -> None:
        """H5/H13: a registry adapter never inherits the Kiro provider contract.

        Both ids are SELECTABLE on a plain public build, so the direct
        factory's unconditional ``AcpProvider`` construction would hand a spec
        adapter the Kiro provider class and the Kiro wire contract. The pick
        degrades to Kiro instead (H3: degrade with a reason, never raise); the
        dialect-correct route is the registry dispatcher, exercised by
        ``test_override_wins_over_the_factory_snapshot``.
        """
        from kiro_crew.acp import backends as acp_backends
        from kiro_crew.acp.types import ACP_BACKEND_KIRO

        # Guard the guard: the premise is that these really are selectable
        # SPEC-dialect ids, so the test cannot pass by the pick being dropped
        # at the selectability gate instead.
        from kiro_crew.acp_backends import selectable_backends

        assert adapter in selectable_backends()
        assert acp_backends.dialect_of(adapter) is acp_backends.Dialect.SPEC

        cfg = KiroCrewConfig()
        provider = build_provider_factory(cfg)(
            session_key="subagent:child",
            acp_backend=adapter,
        )

        assert provider.client.backend == ACP_BACKEND_KIRO

    def test_member_auto_route_cannot_smuggle_a_spec_dialect_in(self) -> None:
        """The same hole by the other arm: agent.member_acp_backend.

        A member DM resolves its backend inside the factory rather than through
        ``session_allocation``'s crossover, so it is the one arm no factory swap
        protects. It must land on Kiro, not on AcpProvider-wrapped copilot.
        """
        from kiro_crew.acp.types import ACP_BACKEND_KIRO
        from kiro_crew.members import DM_SLOT_KEY_PREFIX, is_member_session_key

        cfg = KiroCrewConfig()
        cfg.agent.member_acp_backend = "copilot"
        member_key = f"{DM_SLOT_KEY_PREFIX}alice"
        assert is_member_session_key(member_key)

        provider = build_provider_factory(cfg)(session_key=member_key)

        assert provider.client.backend == ACP_BACKEND_KIRO

    def test_adapter_snapshot_model_is_withheld_from_an_adapter_override(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
        cfg.agent.model = "adapter-global"
        provider = build_provider_factory(cfg)(
            session_key="subagent:child",
            acp_backend=ACP_BACKEND_GOOSE,
            inherit_config_model=False,
        )
        assert provider.client._model == DEFAULT_MODEL

    def test_spec_override_does_not_translate_registry_keys(self) -> None:
        """goose ids are not model_registry keys; translating would invent one."""
        from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_GOOSE

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_CODEX
        provider = build_provider_factory(cfg)(
            session_key="subagent:child",
            acp_backend=ACP_BACKEND_GOOSE,
            model_override="opus-4.8-1m",
        )
        assert provider.client._model == "opus-4.8-1m"

    def test_spec_auto_does_not_resolve_through_kiro_agent_files(self) -> None:
        """Backend-owned namespaces leave auto to the selected adapter."""
        from kiro_crew.acp.types import ACP_BACKEND_GOOSE

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_GOOSE
        with (
            patch.object(
                KiroCrewConfig,
                "_resolve_agent_model",
                side_effect=AssertionError("adapter read the Kiro global agent file"),
            ),
            patch.object(
                KiroCrewConfig,
                "_resolve_named_agent_model",
                side_effect=AssertionError("adapter read a named Kiro agent file"),
            ),
        ):
            provider = build_provider_factory(cfg)(
                session_key="subagent:child",
                agent="reviewer",
            )

        assert provider.client._model == DEFAULT_MODEL

    def test_spec_named_agent_inherits_a_concrete_backend_global(self) -> None:
        """A concrete adapter-global model wins without consulting Kiro pins."""
        from kiro_crew.acp.types import ACP_BACKEND_GOOSE

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_GOOSE
        cfg.agent.model = "backend-owned-model"
        with patch.object(
            KiroCrewConfig,
            "_resolve_named_agent_model",
            side_effect=AssertionError("adapter read a named Kiro agent file"),
        ):
            provider = build_provider_factory(cfg)(
                session_key="subagent:child",
                agent="reviewer",
            )

        assert provider.client._model == "backend-owned-model"

    def test_spec_adapter_uses_admission_provider_at_registry_seam(self) -> None:
        from kiro_crew.acp.client import SpecAdapterAcpClient
        from kiro_crew.acp.types import ACP_BACKEND_GOOSE
        from kiro_crew.providers.acp import SpecAdapterAcpProvider

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_GOOSE

        provider = build_provider_factory(cfg)(session_key="subagent:child")

        assert isinstance(provider, SpecAdapterAcpProvider)
        assert isinstance(provider.client, SpecAdapterAcpClient)

    def test_direct_config_factory_remains_kiro_only(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_GOOSE, ACP_BACKEND_KIRO
        from kiro_crew.providers.acp import AcpProvider

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_GOOSE

        provider = cfg.create_provider_factory()(session_key="subagent:child")

        assert type(provider) is AcpProvider
        assert provider.backend == ACP_BACKEND_KIRO

    def test_direct_config_factory_isolated_from_adapter_factory(self) -> None:
        """An adapter-factory failure must not enter the first-class Kiro path."""

        class AdapterFactoryFailureConfig(KiroCrewConfig):
            def _create_adapter_provider_factory(self, **_kwargs):
                raise RuntimeError("adapter factory reached")

        cfg = AdapterFactoryFailureConfig()

        provider = cfg.create_provider_factory()(session_key="chat:kiro")

        assert provider.backend == ""

    def test_default_registry_delegates_ordinary_kiro_to_the_direct_factory(self) -> None:
        """H13: adapter admission never enters ordinary Kiro construction."""
        from kiro_crew.platform.defaults import DefaultProviderRegistry

        cfg = KiroCrewConfig()
        direct_factory = cfg.create_provider_factory()
        with patch.object(cfg, "create_provider_factory", return_value=direct_factory):
            resolved = DefaultProviderRegistry().create_factory(cfg)

        assert resolved is direct_factory
        provider = resolved(session_key="chat:kiro")

        assert provider.backend == ""

    def test_registry_refuses_an_unmapped_adapter_dialect(self) -> None:
        """A new dialect cannot silently inherit the Kiro provider contract."""
        from kiro_crew.acp import backends
        from kiro_crew.acp.types import ACP_BACKEND_GOOSE

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_GOOSE

        with patch.object(backends, "dialect_of", return_value=object()):
            with pytest.raises(RuntimeError, match="Unsupported ACP dialect"):
                build_provider_factory(cfg)(session_key="chat:unmapped-dialect")

    def test_kiro_path_still_translates_registry_keys(self) -> None:
        from kiro_crew import model_registry
        from kiro_crew.acp.types import ACP_BACKEND_KIRO

        cfg = KiroCrewConfig()
        provider = build_provider_factory(cfg)(
            session_key="subagent:child",
            acp_backend=ACP_BACKEND_KIRO,
            model_override="opus-4.8-1m",
        )
        assert provider.client._model == model_registry.to_acp_id("opus-4.8-1m")
        assert provider.client._model != "opus-4.8-1m"
