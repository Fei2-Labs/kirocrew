"""Typed ACP client seam for SDK-owned long-lived workers."""

from __future__ import annotations

from typing import Any, Protocol


class PromptStats(Protocol):
    @property
    def context_pct(self) -> float: ...


class AcpWorkerClient(Protocol):
    @property
    def backend(self) -> str: ...

    @property
    def is_ready(self) -> bool: ...

    @property
    def spec_zero_tools(self) -> bool: ...

    @property
    def last_prompt_stats(self) -> PromptStats: ...

    async def ensure_ready(self) -> None: ...

    async def shutdown(self) -> None: ...

    def is_process_alive(self) -> bool: ...  # pid-owner-ok: the pool probes its own child

    def authored_spec_declares_zero_tools(self) -> bool: ...

    def effective_spec_declares_zero_tools(self, *, authored_before_spawn: bool) -> bool: ...

    def supports_config_option(self, option_id: str) -> bool: ...

    def get_valid_effort_levels(self) -> list[str] | None: ...

    async def set_config_option(self, option_id: str, value: str) -> Any: ...

    async def send_command(self, command: str, *, args: dict[str, Any]) -> Any: ...

    async def send_message(self, prompt: str, *, timeout: float) -> str: ...


def create_acp_worker_client(
    *,
    agent: str,
    sandbox_mode: str,
    acp_backend: str,
    audit_source: str,
    model: str | None = None,
) -> AcpWorkerClient:
    """Construct an ACP client without exposing its implementation type."""
    from kiro_crew.acp.client import AcpClient

    kwargs: dict[str, Any] = {
        "agent": agent,
        "sandbox_mode": sandbox_mode,
        "acp_backend": acp_backend,
        "audit_source": audit_source,
    }
    if model is not None:
        kwargs["model"] = model
    return AcpClient(**kwargs)
