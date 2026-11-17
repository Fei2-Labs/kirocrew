"""The ACP choices exposed in settings must survive config writes and reloads."""

import pytest

from kiro_crew.acp import backends
from kiro_crew.acp.types import ACP_BACKEND_KIRO


class TestTheCardsOfferAndTheSaveAllowlistAgree:
    """Every adapter the card lets you pick must survive the whole write path.

    Found by clicking the real card in an isolated gateway. THREE independent
    allowlists govern one setting, and they had drifted apart:

      1. selectable_backends()      what the endpoint advertises
      2. _EDITABLE_CONFIG             what the config PATCH will accept
      3. the AgentConfig schema enum  what survives the next config LOAD

    Only (1) had been widened. (2) rejected the write outright with "field not
    editable". Fixing that exposed (3), which was still frozen at ['', 'kas'] —
    and that failed far worse: the PATCH answered 200 and the value was then
    discarded on load, so the UI reported success and nothing changed.

    All three now read (1). These tests exist so a fourth surface cannot quietly
    appear, and so widening the set in one place can never again half-work.
    """

    def test_every_selectable_backend_is_writable(self) -> None:
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        spec = _EDITABLE_CONFIG.get("agent.acp_backend")
        assert spec is not None, (
            "agent.acp_backend is missing from _EDITABLE_CONFIG, so the adapter "
            "card can offer a choice the API refuses to persist"
        )
        # The PATCH allowlist speaks the config-file vocabulary ("kiro" for the
        # empty-string runtime id, "" for "no choice"); backend_from_stored
        # folds both onto the runtime id the selectable set is spelled in.
        from kiro_crew.agent_sdk.backends import backend_from_stored

        writable = {
            (backend_from_stored(v) if backend_from_stored(v) is not None else ACP_BACKEND_KIRO)
            for v in spec["values_fn"]()
        }
        assert writable == set(backends.selectable_ids()), (
            "the writable set and selectable_backends() disagree; a value the "
            "card offers would be refused, or one it hides would be accepted"
        )

    def test_every_selectable_backend_survives_config_validation(self) -> None:
        """The load-time enum must accept every value the PATCH offers."""
        from kiro_crew.config.validation import validate_config_data

        for backend in backends.selectable_ids():
            data = {"agent": {"acp_backend": backend}}
            validate_config_data(data)
            assert data["agent"]["acp_backend"] == backend

    def test_import_time_schema_build_does_not_read_registry_cache(self, monkeypatch) -> None:
        from kiro_crew.acp import registry
        from kiro_crew.config.loader import AgentConfig
        from kiro_crew.config.schema import _build_object_schema

        monkeypatch.setattr(
            registry,
            "cached",
            lambda: (_ for _ in ()).throw(AssertionError("schema build performed filesystem I/O")),
        )
        schema = _build_object_schema(AgentConfig)
        assert "enum" not in schema["properties"]["acp_backend"]

    def test_the_default_backend_is_writable(self) -> None:
        """Switching BACK to kiro-cli has to work too.

        kiro is spelled as the empty string, which is exactly the sort of value an
        allowlist check drops by truthiness.
        """
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        values = _EDITABLE_CONFIG["agent.acp_backend"]["values_fn"]()
        assert ACP_BACKEND_KIRO in set(values)

    def test_registry_discovery_alone_does_not_widen_the_admission_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.acp import registry
        from kiro_crew.config.schema import JSON_SCHEMA

        adapter = registry.RegistryAdapter(
            id="late-registry-acp",
            name="Late Registry ACP",
            version="1.0.0",
            description="",
            repository="",
            license="MIT",
            icon="",
            kind="npx",
            package="late-registry-acp@1.0.0",
            args=(),
            env=(),
        )
        frozen = JSON_SCHEMA["properties"]["agent"]["properties"]["acp_backend"]
        assert "enum" not in frozen
        monkeypatch.setattr(registry, "cached", lambda: {adapter.id: adapter})
        assert adapter.id not in backends.selectable_ids()
