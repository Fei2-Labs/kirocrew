"""Tests for the sandbox off-loop shields: ``sandboxed_spawn_argv_off_loop``
(the fork's original wrapper, still the direct chokepoint for three call
sites) and ``shielded_prepare_off_loop`` (the shared shield every other site
was consolidated onto).

Cancel-injection tests: verify that cancelling the awaiting coroutine while the
worker is still inside ``sandboxed_spawn_argv`` recovers the cleanup path and
unlinks the temp launcher/profile, rather than leaking it. Pinned for both
wrappers, since both are live production code paths.

Executor test: the caller's pool is honoured, so consolidating the shield does
not silently collapse each site's ``executors.py`` pool onto the default one.

AST guard test: scans ``src/kiro_crew/`` for bare ``run_in_executor`` AND
``asyncio.to_thread`` hops referencing ``sandboxed_spawn_argv`` (the sync
version) without going through a shield. Prevents regressions.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import os
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the source tree is importable without pip install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kiro_crew.sandbox import (  # noqa: E402
    sandboxed_spawn_argv_off_loop,
    shielded_prepare_off_loop,
)


class _CapturedExecutor:
    """Executor test double whose future can be settled by the test."""

    def __init__(self, *, run_first: bool = False):
        self.future: Future = Future()
        self.submitted = threading.Event()
        self._preparation_submitted = False
        self._run_first = run_first

    def submit(self, function, *args):
        if not self._preparation_submitted:
            self._preparation_submitted = True
            self.submitted.set()
            if self._run_first:
                threading.Thread(target=self._run, args=(function, args), daemon=True).start()
            return self.future
        result: Future = Future()

        threading.Thread(target=self._run, args=(function, args, result), daemon=True).start()
        return result

    def _run(self, function, args, result=None) -> None:
        try:
            value = function(*args)
            if result is not None:
                result.set_result(value)
            elif not self.future.done():
                self.future.set_result(value)
        except BaseException as error:
            if result is not None:
                result.set_exception(error)
            elif not self.future.done():
                self.future.set_exception(error)


def _prepare(argv: list[str]):
    """Bind the chokepoint the way every production call site does."""
    from kiro_crew import sandbox

    return functools.partial(sandbox.sandboxed_spawn_argv, argv)


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
        with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=fake):
            task = asyncio.create_task(sandboxed_spawn_argv_off_loop(["/bin/true"]))
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
        assert not any(f.exists() for f in created), "launcher was NOT cleaned up on cancellation"

    @pytest.mark.asyncio
    async def test_cancel_during_hop_drops_launcher_via_shared_shield(self, tmp_path):
        """Same guarantee, through ``shielded_prepare_off_loop`` — the wrapper
        every OTHER call site was consolidated onto."""
        fake, entered, release, created = self._blocking_sandbox(tmp_path)
        with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=fake):
            task = asyncio.create_task(shielded_prepare_off_loop(_prepare(["/bin/true"])))
            await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert created, "stub was never called"
        assert not any(f.exists() for f in created), "launcher was NOT cleaned up on cancellation"

    @pytest.mark.asyncio
    async def test_cancel_on_a_caller_pool_still_drops_launcher(self, tmp_path):
        """The recovery works on a caller-supplied pool, not just the default one."""
        fake, entered, release, created = self._blocking_sandbox(tmp_path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=fake):
                task = asyncio.create_task(
                    shielded_prepare_off_loop(_prepare(["/bin/true"]), executor=pool)
                )
                await asyncio.to_thread(entered.wait, 10)
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert created, "stub was never called"
        assert not any(f.exists() for f in created), "launcher was NOT cleaned up on cancellation"

    @pytest.mark.asyncio
    async def test_the_callers_executor_is_the_one_used(self):
        """Pool choice stays with the caller — the whole point of the parameter.

        Pinned by the worker thread's identity: the preparation must run on a
        thread belonging to the pool that was passed in, not on the loop's
        default executor.
        """
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pinned-pool") as pool:
            names: list[str] = []

            def _fake(argv, mode="standard", **_kwargs):
                names.append(threading.current_thread().name)
                return argv, {}, None

            with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=_fake):
                await shielded_prepare_off_loop(_prepare(["/bin/true"]), executor=pool)
        assert names and names[0].startswith("pinned-pool"), names


class TestShieldedPrepareOffLoop:
    """Pin the shared preparation wrapper's cancellation and cleanup contract."""

    @pytest.mark.asyncio
    async def test_normal_completion_returns_preparation_result(self):
        result = (["wrapped"], {"PATH": "/usr/bin"}, None)

        assert await shielded_prepare_off_loop(lambda: result) == result

    @pytest.mark.asyncio
    async def test_cancellation_after_preparation_settles_still_unlinks(self, tmp_path):
        cleanup = tmp_path / "launcher"
        cleanup.write_text("profile")
        executor = _CapturedExecutor()
        task = asyncio.create_task(
            shielded_prepare_off_loop(lambda: (["wrapped"], {}, str(cleanup)), executor=executor)
        )

        # Let the wrapper submit its work, then cancel from the future's done
        # callback. This makes the cancellation happen after preparation has
        # settled while the wrapper is still suspended at its shielded await.
        await asyncio.to_thread(executor.submitted.wait, 10)
        executor.future.add_done_callback(lambda _future: task.cancel())
        executor.future.set_result((["wrapped"], {}, str(cleanup)))
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not cleanup.exists()

    @pytest.mark.asyncio
    async def test_repeated_cancellation_during_recovery_still_unlinks(self, tmp_path):
        cleanup = tmp_path / "launcher"
        cleanup.write_text("profile")
        prepared = threading.Event()
        release_prepare = threading.Event()
        entered_unlink = threading.Event()
        release_unlink = threading.Event()
        executor = _CapturedExecutor(run_first=True)
        real_unlink = os.unlink

        def prepare():
            prepared.set()
            assert release_prepare.wait(timeout=10)
            return ["wrapped"], {}, str(cleanup)

        def unlink(path):
            entered_unlink.set()
            assert release_unlink.wait(timeout=10)
            real_unlink(path)

        with patch("kiro_crew.sandbox.os.unlink", side_effect=unlink):
            task = asyncio.create_task(shielded_prepare_off_loop(prepare, executor=executor))
            await asyncio.to_thread(prepared.wait, 10)
            task.cancel()
            release_prepare.set()
            await asyncio.to_thread(entered_unlink.wait, 10)
            task.cancel()
            task.cancel()
            release_unlink.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert not cleanup.exists()

    @pytest.mark.asyncio
    async def test_normal_return_passes_through(self, tmp_path):
        """Non-cancelled calls pass through the chokepoint result unchanged."""
        launcher = tmp_path / "sb-launcher"
        launcher.write_text("# fake")

        def _fake(argv, mode="standard", **_kwargs):
            return ["wrapped"] + argv, {"PATH": "/usr/bin"}, str(launcher)

        with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=_fake):
            result = await shielded_prepare_off_loop(_prepare(["/bin/true"]))
        assert result == (
            ["wrapped", "/bin/true"],
            {"PATH": "/usr/bin"},
            str(launcher),
        )


class TestSandboxedSpawnArgvOffLoopPassthrough:
    """The older wrapper (still the direct chokepoint for three call sites)
    keeps the same pass-through contract as the shared shield."""

    @pytest.mark.asyncio
    async def test_normal_return_passes_through(self, tmp_path):
        launcher = tmp_path / "sb-launcher"
        launcher.write_text("# fake")

        def _fake(argv, mode="standard", **_kwargs):
            return ["wrapped"] + argv, {"PATH": "/usr/bin"}, str(launcher)

        with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=_fake):
            result = await sandboxed_spawn_argv_off_loop(["/bin/true"])
        assert result == (
            ["wrapped", "/bin/true"],
            {"PATH": "/usr/bin"},
            str(launcher),
        )


class TestNoBareSandboxedSpawnArgvHops:
    """AST guard: no async code may hop to the sync chokepoint without a shield.

    Every async caller must go through ``shielded_prepare_off_loop`` so a
    cancelled awaiter cannot abandon the worker's tuple and leak the launcher.

    Two things this guard has to get right, or it passes vacuously:

    * BOTH spellings of a bare hop -- ``loop.run_in_executor(pool, fn, ...)``
      and ``asyncio.to_thread(fn, ...)``. The private copies this PR collapsed
      used the second one, so covering only the first closes half the class.
    * ONE LEVEL OF INDIRECTION. Most sites do not hand the chokepoint to the
      hop directly; they hand it a module-level preparer that calls it
      (``_prepare_probe``, ``_sandboxed``, ``_prepare_git_spawn``), whose name
      says nothing about the chokepoint. A guard matching only the literal
      ``sandboxed_spawn_argv`` at the hop site is blind to exactly the sites
      this PR had to fix, so per file we first collect the functions that call
      the chokepoint and treat a hop to any of them as a hop to it.

    One exemption remains: ``sandbox.py``'s own definition of
    ``sandboxed_spawn_argv_off_loop``. That wrapper predates ``shielded_prepare_off_loop``
    and is deliberately kept alive (three call sites — ``dev_fleet``, papyrus's
    ``gitops``/``latex`` — carry extensive test coverage keyed to its name), so its
    internal hop IS the shield, not a bypass of one.
    """

    _CHOKEPOINT = "sandboxed_spawn_argv"
    _EXEMPT_FILES = frozenset({"sandbox.py"})

    @staticmethod
    def _src_root() -> Path:
        return Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

    def _python_files(self) -> list[Path]:
        root = self._src_root()
        return sorted(root.rglob("*.py"))

    @classmethod
    def _names_that_reach_the_chokepoint(cls, tree: ast.Module) -> set[str]:
        """Names a hop may be handed that end up calling the chokepoint.

        The chokepoint itself, plus every function in this module whose body
        calls it — the preparer indirection every real call site uses.
        """
        reaching = {cls._CHOKEPOINT}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                fn = child.func
                hits = (isinstance(fn, ast.Name) and fn.id == cls._CHOKEPOINT) or (
                    isinstance(fn, ast.Attribute) and fn.attr == cls._CHOKEPOINT
                )
                if hits:
                    reaching.add(node.name)
                    break
        return reaching

    @staticmethod
    def _is_bare_hop_call(node: ast.Call) -> bool:
        """Return True for a ``*.run_in_executor(...)`` or ``*.to_thread(...)`` call."""
        func = node.func
        return isinstance(func, ast.Attribute) and func.attr in {
            "run_in_executor",
            "to_thread",
        }

    @staticmethod
    def _references_any(node: ast.Call, names: set[str]) -> str | None:
        """The first of ``names`` referenced anywhere in the call's arguments.

        Walks each argument, so a ``functools.partial(fn, ...)`` wrapper and a
        plain reference are both caught.
        """
        for arg in [*node.args, *[kw.value for kw in node.keywords]]:
            for child in ast.walk(arg):
                if isinstance(child, ast.Name) and child.id in names:
                    return child.id
                if isinstance(child, ast.Attribute) and child.attr in names:
                    return child.attr
        return None

    def test_no_bare_hops_to_sandboxed_spawn_argv(self):
        violations: list[str] = []
        for path in self._python_files():
            rel = path.relative_to(self._src_root())
            if rel.name in self._EXEMPT_FILES:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            reaching = self._names_that_reach_the_chokepoint(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not self._is_bare_hop_call(node):
                    continue
                hit = self._references_any(node, reaching)
                if hit is not None:
                    violations.append(f"{rel}:{node.lineno}: bare hop to {hit}")
        assert not violations, (
            "Found bare run_in_executor / to_thread hops that reach sandboxed_spawn_argv "
            "(must go through sandbox.shielded_prepare_off_loop):\n" + "\n".join(violations)
        )
