"""Tests for ``sandboxed_spawn_argv_off_loop`` — the shared shielded wrapper.

Cancel-injection test: verifies that cancelling the awaiting coroutine while the
worker thread is still inside ``sandboxed_spawn_argv`` recovers the cleanup path
and unlinks the temp launcher/profile, rather than leaking it.

AST guard test: scans ``src/kiro_crew/`` for bare ``run_in_executor`` calls whose
callable argument references ``sandboxed_spawn_argv`` (the sync version) without
going through the shielded ``_off_loop`` wrapper. Prevents regressions.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the source tree is importable without pip install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kiro_crew.sandbox import sandboxed_spawn_argv_off_loop  # noqa: E402


class TestSandboxOffLoopCancellation:
    """Cancellation during the worker hop must not orphan the temp launcher."""

    @staticmethod
    def _blocking_sandbox(tmp_path):
        """A ``sandboxed_spawn_argv`` stub that parks the worker until released.

        Reproduces the race deterministically: the awaiting coroutine is
        cancelled while the thread is still inside the chokepoint, and the
        launcher only comes into being AFTER the cancellation landed.
        """
        entered = threading.Event()
        release = threading.Event()
        created: list[Path] = []

        def _fake(argv, mode="standard", **_kwargs):
            entered.set()
            assert release.wait(timeout=10), "test never released the worker"
            launcher = tmp_path / f"sb-launcher-{len(created)}"
            launcher.write_text("# fake sandbox launcher/profile")
            created.append(launcher)
            return argv, {}, str(launcher)

        return _fake, entered, release, created

    @pytest.mark.asyncio
    async def test_cancel_during_hop_drops_launcher(self, tmp_path):
        fake, entered, release, created = self._blocking_sandbox(tmp_path)
        with patch(
            "kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=fake
        ):
            task = asyncio.create_task(
                sandboxed_spawn_argv_off_loop(["/bin/true"])
            )
            # Wait for the worker to enter the stub (implies awaiter is
            # suspended at the shielded hop).
            await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        # The launcher was created by the worker thread and should have been
        # unlinked by the recovery path in the CancelledError handler.
        assert created, "stub was never called"
        assert not any(f.exists() for f in created), (
            "launcher was NOT cleaned up on cancellation"
        )

    @pytest.mark.asyncio
    async def test_normal_return_passes_through(self, tmp_path):
        """Non-cancelled calls pass through the chokepoint result unchanged."""
        launcher = tmp_path / "sb-launcher"
        launcher.write_text("# fake")

        def _fake(argv, mode="standard", **_kwargs):
            return ["wrapped"] + argv, {"PATH": "/usr/bin"}, str(launcher)

        with patch(
            "kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=_fake
        ):
            result = await sandboxed_spawn_argv_off_loop(["/bin/true"])
        assert result == (
            ["wrapped", "/bin/true"],
            {"PATH": "/usr/bin"},
            str(launcher),
        )


class TestNoBareSandboxedSpawnArgvHops:
    """AST guard: no async code may use bare run_in_executor with the sync chokepoint.

    Every async caller must use ``sandboxed_spawn_argv_off_loop`` (or an
    equivalent local shield pattern) so cancellation cannot leak the launcher.
    This test scans the source tree and fails if a new unshielded hop appears.
    """

    # Modules with their own cleanup mechanism that are exempt from the guard.
    _EXEMPT_PATHS = frozenset({
        # spec_builder shields the cleanup unlink in its own finally block.
        "spec_builder",
    })

    @staticmethod
    def _src_root() -> Path:
        return Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

    def _python_files(self) -> list[Path]:
        root = self._src_root()
        return sorted(root.rglob("*.py"))

    @staticmethod
    def _is_run_in_executor_call(node: ast.Call) -> bool:
        """Return True if node is a call to `*.run_in_executor(...)`."""
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "run_in_executor"
        )

    @staticmethod
    def _references_sandboxed_spawn(node: ast.Call) -> bool:
        """Return True if any argument references ``sandboxed_spawn_argv``.

        Catches both direct references (``sandboxed_spawn_argv``) and
        ``functools.partial(sandboxed_spawn_argv, ...)``.
        """
        for arg in [*node.args, *[kw.value for kw in node.keywords]]:
            for child in ast.walk(arg):
                if isinstance(child, ast.Name) and child.id == "sandboxed_spawn_argv":
                    return True
                if (
                    isinstance(child, ast.Attribute)
                    and child.attr == "sandboxed_spawn_argv"
                ):
                    return True
        # Also check the callable itself (2nd positional arg to run_in_executor)
        if len(node.args) >= 2:
            target = node.args[1]
            for child in ast.walk(target):
                if isinstance(child, ast.Name) and child.id == "sandboxed_spawn_argv":
                    return True
                if (
                    isinstance(child, ast.Attribute)
                    and child.attr == "sandboxed_spawn_argv"
                ):
                    return True
        return False

    def test_no_bare_executor_hops_to_sandboxed_spawn_argv(self):
        violations: list[str] = []
        for path in self._python_files():
            rel = path.relative_to(self._src_root())
            if any(exempt in str(rel) for exempt in self._EXEMPT_PATHS):
                continue
            try:
                tree = ast.parse(path.read_text(), filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if (
                    self._is_run_in_executor_call(node)
                    and self._references_sandboxed_spawn(node)
                ):
                    violations.append(
                        f"{rel}:{node.lineno}: bare run_in_executor "
                        f"with sandboxed_spawn_argv"
                    )
        assert not violations, (
            "Found bare run_in_executor calls to sandboxed_spawn_argv "
            "(must use sandboxed_spawn_argv_off_loop or a local shield):\n"
            + "\n".join(violations)
        )
