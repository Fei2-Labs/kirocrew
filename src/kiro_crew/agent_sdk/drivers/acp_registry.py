"""ACP registry operations that expose only plain data or provider builders."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def validate_backend(backend: str) -> None:
    """Raise when *backend* has no descriptor in the active ACP registry."""
    from kiro_crew.acp import backends

    backends.descriptor_for(backend)


def adapter_factory_capabilities(backend: str) -> tuple[bool, bool]:
    """Return whether an adapter uses registry model ids and supports Tool Search."""
    from kiro_crew.acp import backends

    return (
        backends.supports(backend, backends.CAP_REGISTRY_MODEL_IDS),
        backends.supports(backend, backends.CAP_TOOL_SEARCH),
    )


def backend_speaks_kiro_dialect(backend: str) -> bool:
    """Whether *backend* speaks kiro's ACP dialect; an id with no descriptor does not."""
    from kiro_crew.acp import backends

    try:
        return backends.dialect_of(backend) is backends.Dialect.KIRO
    except backends.UnknownAcpBackend:
        return False


def adapter_provider_builder(backend: str) -> Callable[..., Any]:
    """Build a closure that constructs the provider implementation for *backend*."""
    from kiro_crew.acp import backends
    from kiro_crew.providers.acp import AcpProvider, SpecAdapterAcpProvider

    backends.descriptor_for(backend)
    provider_types = {
        backends.Dialect.KIRO: AcpProvider,
        backends.Dialect.SPEC: SpecAdapterAcpProvider,
    }
    dialect = backends.dialect_of(backend)
    try:
        provider_type = provider_types[dialect]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported ACP dialect {dialect!r} for backend {backend!r}") from exc

    def build(**kwargs: Any) -> Any:
        return provider_type(**kwargs)

    return build


def harness_status(backend: str) -> tuple[str, bool]:
    """Return the display label and Kiro-credit billing status for a backend."""
    from kiro_crew.acp import backends

    try:
        label = backends.descriptor_for(backend).label
    except backends.UnknownAcpBackend:
        label = backend
    return label, backends.bills_kiro_credits(backend)
