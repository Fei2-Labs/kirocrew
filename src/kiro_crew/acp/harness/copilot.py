"""The ``copilot`` harness resolver: where the GitHub Copilot CLI lives.

Fork-owned: upstream has no ``copilot`` backend, so this module carries the
whole resolver. It follows the per-harness module scheme of
:mod:`kiro_crew.acp.harness.claude` so a future upstream adoption has one file
to compare against. Spawn resolves the CLI per session; nothing here is cached
across spawns, so an install or an uninstall is picked up by the next session.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_COPILOT
from kiro_crew.env import augmented_path

__all__ = [
    "COPILOT_ACP_ARG",
    "COPILOT_BIN",
    "CopilotLaunch",
]

#: The executable name the GitHub Copilot CLI ships under.
COPILOT_BIN = "copilot"

#: The CLI flag that puts the GitHub Copilot CLI into ACP stdio mode.
COPILOT_ACP_ARG = "--acp"


def _resolve_copilot_bin() -> str | None:
    """Find the GitHub Copilot CLI executable for the ``copilot`` ACP backend.

    Resolution order:
      1. ``COPILOT_ACP_BIN`` env var (explicit override).
      2. Augmented PATH (covers a plain ``npm i -g @github/copilot``, winget,
         Homebrew, or the install-script layouts).
      3. The per-user install dir the GitHub Copilot CLI's own
         installer/updater uses (``github-copilot-sdk/cli/<version>/``), newest
         version first — present even when the CLI was launched via a desktop
         app wrapper that never put it on PATH.
    """
    override = os.environ.get("COPILOT_ACP_BIN")
    if override and Path(override).is_file():
        return override

    search_path = augmented_path(os.environ.get("PATH", ""))
    on_path = shutil.which(COPILOT_BIN, path=search_path)
    if on_path:
        return on_path

    sdk_root = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        / "github-copilot-sdk"
        / "cli"
    )
    if sdk_root.is_dir():
        exe_name = "copilot.exe" if platform_compat.IS_WINDOWS else "copilot"
        candidates = sorted(
            (p for p in sdk_root.glob(f"*/{exe_name}") if p.is_file()),
            key=lambda p: p.parent.name,
            reverse=True,
        )
        if candidates:
            return str(candidates[0])

    return None


class CopilotLaunch(ProcessAdapter):
    """copilot: the GitHub Copilot CLI's own ``--acp`` stdio mode, one process per session.

    Bare by design: the CLI carries its own account (BYOK), mounts no Crew MCP
    array (it is outside ``ACP_BACKENDS_SESSION_MCP_ARRAY``), and answers tool
    calls through its native ``session/request_permission`` route rather than a
    gate this adapter would seed -- so the plan is the binary and its flag, and
    nothing else.
    """

    backend = ACP_BACKEND_COPILOT

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        # Read through the client at call time: its unit tests rebind
        # ``kiro_crew.acp.client._resolve_copilot_bin`` under the launch capture
        # (the same seam ``harness.pi`` reads ``_mise_which`` through).
        from kiro_crew.acp import client as client_mod

        copilot_bin = await asyncio.to_thread(client_mod._resolve_copilot_bin)
        if not copilot_bin:
            raise AcpError(
                f"{COPILOT_BIN} not found. Install GitHub Copilot CLI "
                "(https://docs.github.com/copilot/how-tos/copilot-cli/"
                "set-up-copilot-cli/install-copilot-cli) and make sure it "
                "is on PATH, or set COPILOT_ACP_BIN to its executable path."
            )
        return SpawnPlan(
            argv=[copilot_bin, COPILOT_ACP_ARG],
            spawn_label=f"{COPILOT_BIN} {COPILOT_ACP_ARG}",
            stderr_label=COPILOT_BIN,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """copilot adds no host variables: BYOK auth is the CLI's own account."""
