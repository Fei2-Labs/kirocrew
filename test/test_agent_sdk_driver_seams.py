"""Provider construction and vocabulary exposed through the Agent SDK."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.agent_sdk import create_acp_worker_client, is_text_chunk_event
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.drivers.acp_registry import adapter_provider_builder, harness_status


@pytest.mark.parametrize(
    ("model", "expected"),
    [(None, {}), ("auto", {"model": "auto"})],
    ids=["omit-unset-model", "forward-selected-model"],
)
def test_worker_client_factory_only_passes_a_selected_model(model, expected):
    kwargs = {
        "agent": "worker",
        "sandbox_mode": "standard",
        "acp_backend": ACP_BACKEND_KIRO,
        "audit_source": "subagent",
        **expected,
    }
    with patch("kiro_crew.acp.client.AcpClient") as client_type:
        client = create_acp_worker_client(
            agent="worker",
            sandbox_mode="standard",
            acp_backend=ACP_BACKEND_KIRO,
            audit_source="subagent",
            model=model,
        )

    assert client is client_type.return_value
    client_type.assert_called_once_with(**kwargs)


@pytest.mark.parametrize(
    "backend",
    [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE],
)
def test_adapter_provider_builder_selects_the_registered_dialect(backend, monkeypatch):
    from kiro_crew.providers import acp as providers

    class KiroProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class SpecProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(providers, "AcpProvider", KiroProvider)
    monkeypatch.setattr(providers, "SpecAdapterAcpProvider", SpecProvider)
    builder = adapter_provider_builder(backend)

    provider = builder(session_key="test:session")

    expected_type = KiroProvider if backend == ACP_BACKEND_KIRO else SpecProvider
    assert isinstance(provider, expected_type)
    assert provider.kwargs == {"session_key": "test:session"}


def test_text_chunk_event_helper_matches_provider_vocabulary():
    assert is_text_chunk_event("text_chunk")
    assert not is_text_chunk_event("tool_call")


def test_harness_status_falls_back_only_for_an_unknown_backend(monkeypatch):
    from kiro_crew.acp import backends

    def unknown(_backend):
        raise backends.UnknownAcpBackend("not registered")

    monkeypatch.setattr(backends, "descriptor_for", unknown)

    assert harness_status("not-registered") == ("not-registered", False)


def test_harness_status_surfaces_registry_errors(monkeypatch):
    from kiro_crew.acp import backends

    def unavailable(_backend):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(backends, "descriptor_for", unavailable)

    with pytest.raises(RuntimeError, match="registry unavailable"):
        harness_status(ACP_BACKEND_KIRO)
