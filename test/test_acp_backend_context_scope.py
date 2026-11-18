"""Context injection: dialect concerns vs claude-specific branding.

A public-ACP-spec adapter reads no Kiro Crew agent config, so anything kiro-cli
would have loaded from the agent spec — steering resources, mapped skill globs —
must be injected into the prompt instead. That is a DIALECT question and applies
to every spec adapter.

The persona-prompt rewrite is not: it substitutes Claude's own product wording,
so it stays claude-only. These were one flag; conflating them would either
withhold skills from Codex or apply Claude branding to it.
"""

from __future__ import annotations

from kiro_crew.acp.types import (
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_CODEX,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_KAS,
    SPEC_ADAPTER_PROVIDER_LABELS,
)
from kiro_crew.context import _skills_injection_plan


class TestSpecAdapterLabelSet:
    def test_both_adapters_are_members(self) -> None:
        assert PROVIDER_LABEL_CLAUDE in SPEC_ADAPTER_PROVIDER_LABELS
        assert PROVIDER_LABEL_CODEX in SPEC_ADAPTER_PROVIDER_LABELS

    def test_kiro_dialect_labels_are_not_members(self) -> None:
        """KAS speaks kiro's dialect and DOES load the agent config.

        Including it would double-inject skills it already has.
        """
        assert PROVIDER_LABEL_DEFAULT not in SPEC_ADAPTER_PROVIDER_LABELS
        assert PROVIDER_LABEL_KAS not in SPEC_ADAPTER_PROVIDER_LABELS


class TestSkillsInjectionPlan:
    def test_a_custom_agent_injects_the_same_on_every_backend(self) -> None:
        """kiro-cli loads the agent's own skill globs; a spec adapter cannot.

        The plan therefore must not narrow on the vendor flag: a Claude-only
        gate is how Codex and the other spec adapters would silently lose a
        custom agent's skills.
        """
        claude = _skills_injection_plan("some-agent", is_cc=True)
        other = _skills_injection_plan("some-agent", is_cc=False)
        assert claude == other

    def test_the_default_agent_injects_regardless_of_backend(self) -> None:
        """Kiro Crew's own agent has no spec-provided globs to defer to."""
        assert _skills_injection_plan(None, is_cc=False)[0] is True
        assert _skills_injection_plan(None, is_cc=True)[0] is True


class TestBrandingStaysClaudeOnly:
    def test_only_the_claude_label_is_claude_code(self) -> None:
        """The persona rewrite must not widen to every spec adapter.

        Rewriting kiro-cli branding to Claude's wording on a Codex session would
        tell the user they are talking to the wrong product, so the gate that
        selects it answers True for the one Claude label only.
        """
        from kiro_crew.agent_sdk.provider_identity import is_claude_code

        assert is_claude_code(PROVIDER_LABEL_CLAUDE)
        for label in (PROVIDER_LABEL_CODEX, PROVIDER_LABEL_DEFAULT, PROVIDER_LABEL_KAS):
            assert not is_claude_code(label)
