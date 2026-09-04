"""Regression guard for the bundled ``kirocrew`` launcher's symlink resolution.

Background
----------
``packaging/build-desktop.sh`` writes a small bash launcher into every backend
bundle at ``<bundle>/bin/kirocrew``.  It derives its own directory in order to
exec the interpreter sitting next to it (``$DIR/python3.12``).  The naive form::

    DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

is wrong whenever the launcher is reached through a symlink, because
``${BASH_SOURCE[0]}`` is the *symlink* path, not its target.  The gateway plants
exactly such a symlink at ``~/.local/bin/kirocrew`` on every start
(``agent.ensure_kirocrew_on_path``), so on a packaged install ``$DIR`` became
``~/.local/bin`` and the launcher exec'd a non-existent
``~/.local/bin/python3.12`` — every ``kirocrew ...`` invocation from a shell
failed (issue #845).  The fix (#188) walks the symlink chain first.

Why this test exists
--------------------
The shipped fix had no test.  ``resolver_gate`` in the same script only checks
that ``find-bin.js`` and the builder agree on the launcher's *path*; nothing ever
invoked the launcher *through a symlink*, which is the failing mode users hit.
So a revert to the naive one-liner would keep the whole suite green and break the
CLI again on the next desktop build.

These tests extract the launcher from the shipped script (not a copy, so a revert
is what runs here), drop it into a fake bundle next to a stub ``python3.12`` that
reports the directory it was exec'd from, and invoke it through the symlink
shapes a real install produces: a direct call, an absolute symlink from another
directory, a relative symlink, and a chain of symlinks.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the POSIX launcher is a bash script; Windows ships kirocrew.cmd instead",
)

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"


def _extract_launcher() -> str:
    """Pull the launcher heredoc body out of the shipped script.

    Extraction rather than a hard-coded copy: editing or reverting the real
    launcher is what these tests then exercise.
    """
    text = SCRIPT.read_text()
    m = re.search(
        r"cat > \"\$out/bin/kirocrew\" <<'LAUNCH'\n(.*?)\nLAUNCH\n",
        text,
        re.DOTALL,
    )
    assert m, "launcher heredoc not found in packaging/build-desktop.sh"
    return m.group(1)


def _make_bundle(root: Path) -> Path:
    """Create a fake backend bundle: the real launcher + a stub interpreter.

    The stub stands in for the bundled CPython.  It prints the directory it was
    exec'd from, which is precisely the value the launcher computed for ``$DIR``,
    plus the arguments it received — so a test can assert both the resolution and
    the argument forwarding without a 200 MB interpreter.
    """
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)

    launcher = bin_dir / "kirocrew"
    launcher.write_text(_extract_launcher() + "\n")
    launcher.chmod(0o755)

    stub = bin_dir / "python3.12"
    stub.write_text(
        "#!/bin/bash\n"
        # $0 is the path the launcher exec'd, i.e. "$DIR/python3.12".
        'echo "DIR=$(cd "$(dirname "$0")" && pwd)"\n'
        'echo "ARGS=$*"\n'
    )
    stub.chmod(0o755)
    return launcher


def _invoke(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(path), *args],
        capture_output=True,
        text=True,
    )


def test_resolves_when_invoked_directly(tmp_path):
    """Baseline: called by its real path, the launcher finds its sibling."""
    bundle = tmp_path / "kirocrew-backend-arm64"
    launcher = _make_bundle(bundle)

    proc = _invoke(launcher)

    assert proc.returncode == 0, proc.stderr
    assert f"DIR={bundle / 'bin'}" in proc.stdout, proc.stdout


def test_resolves_through_absolute_symlink(tmp_path):
    """The issue #845 case: reached via the PATH symlink the gateway plants.

    This is the assertion that goes red on a revert to the naive
    ``dirname "${BASH_SOURCE[0]}"`` form, which would resolve ``$DIR`` to the
    symlink's own directory and exec a python3.12 that does not exist there.
    """
    bundle = tmp_path / "app" / "backend-dist" / "kirocrew-backend-arm64"
    launcher = _make_bundle(bundle)

    # Mirrors ~/.local/bin/kirocrew -> .../backend-dist/.../bin/kirocrew.
    local_bin = tmp_path / "home" / ".local" / "bin"
    local_bin.mkdir(parents=True)
    shim = local_bin / "kirocrew"
    shim.symlink_to(launcher)

    # Guard the premise: no interpreter next to the symlink, so a launcher that
    # resolves $DIR to the symlink's directory cannot accidentally pass.
    assert not (local_bin / "python3.12").exists()

    proc = _invoke(shim)

    assert proc.returncode == 0, (
        f"launcher failed when invoked through a PATH symlink (issue #845): {proc.stderr}"
    )
    assert f"DIR={bundle / 'bin'}" in proc.stdout, (
        "launcher must resolve $DIR to the real bundle bin dir, not the symlink's dir; "
        f"got: {proc.stdout}"
    )


def test_resolves_through_relative_symlink(tmp_path):
    """A symlink whose target is *relative* must be joined against the link's dir.

    ``ln -s`` records exactly what it is given, so a relative target is a real
    shape on disk.  This covers the launcher's
    ``[ "${SOURCE:0:1}" != "/" ] && SOURCE="$DIR/$SOURCE"`` branch, which is what
    keeps the chain walk from producing a bare, unresolvable filename.
    """
    bundle = tmp_path / "bundle"
    _make_bundle(bundle)

    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    shim = shim_dir / "kirocrew"
    # Relative to shim_dir: ../bundle/bin/kirocrew
    shim.symlink_to(Path("..") / "bundle" / "bin" / "kirocrew")

    proc = _invoke(shim)

    assert proc.returncode == 0, proc.stderr
    assert f"DIR={bundle / 'bin'}" in proc.stdout, proc.stdout


def test_resolves_through_symlink_chain(tmp_path):
    """Multi-hop chains must be walked to the end, not just one level.

    Two hops occur in practice — e.g. a package manager's bin shim pointing at
    ``~/.local/bin/kirocrew``, which points at the bundle — so a single
    ``readlink`` (rather than the ``while`` loop) is not enough.
    """
    bundle = tmp_path / "bundle"
    launcher = _make_bundle(bundle)

    hop1_dir = tmp_path / "hop1"
    hop1_dir.mkdir()
    hop1 = hop1_dir / "kirocrew"
    hop1.symlink_to(launcher)

    hop2_dir = tmp_path / "hop2"
    hop2_dir.mkdir()
    hop2 = hop2_dir / "kirocrew"
    hop2.symlink_to(hop1)

    proc = _invoke(hop2)

    assert proc.returncode == 0, proc.stderr
    assert f"DIR={bundle / 'bin'}" in proc.stdout, proc.stdout


def test_forwards_arguments_through_symlink(tmp_path):
    """Resolution is worthless if the argv is mangled on the way through.

    Asserts the module invocation contract (``-B -s -m kiro_crew``) and that a
    quoted argument containing a space survives as ONE argument.
    """
    bundle = tmp_path / "bundle"
    launcher = _make_bundle(bundle)
    shim = tmp_path / "kirocrew"
    shim.symlink_to(launcher)

    proc = _invoke(shim, "config", "set", "a b")

    assert proc.returncode == 0, proc.stderr
    assert "ARGS=-s -m kiro_crew config set a b" in proc.stdout, proc.stdout

    # And the word-splitting guard: "$@" (not $@) keeps "a b" a single argv entry.
    stub = bundle / "bin" / "python3.12"
    stub.write_text('#!/bin/bash\nprintf "COUNT=%s\\n" "$#"\n')
    stub.chmod(0o755)
    proc = _invoke(shim, "config", "set", "a b")
    assert proc.returncode == 0, proc.stderr
    # -s, -m, kiro_crew, config, set, "a b" == 6
    assert "COUNT=6" in proc.stdout, proc.stdout


def test_launcher_keeps_the_symlink_walk(tmp_path):
    """Wiring guard: the shipped launcher must still contain the chain walk.

    The behavioural tests above already fail on a revert, but this asserts the
    mechanism directly and forbids the exact naive one-liner the bug shipped, so
    the reason for the failure is unambiguous when someone edits the heredoc.
    """
    launcher = _extract_launcher()

    assert 'while [ -h "$SOURCE" ]' in launcher, (
        "launcher lost its symlink-chain walk (issue #845 regression)"
    )
    assert "readlink" in launcher, "launcher must readlink its way to the real path"
    # `cd -P` (physical) is what makes the final dirname immune to a symlinked
    # parent directory; `cd` alone would keep the logical path.
    assert 'cd -P "$(dirname "$SOURCE")"' in launcher, (
        "final DIR must be computed with `cd -P` so a symlinked parent resolves"
    )
    assert 'DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' not in launcher, (
        "launcher reverted to the naive BASH_SOURCE form that broke issue #845"
    )


def _argv_literals_with_isolation() -> list[tuple[Path, int, list[str]]]:
    """Every list/tuple literal under ``src/kiro_crew`` that carries ``-I``.

    Parsed with :mod:`ast`, which drops comments for free — so the prose
    explaining the policy can never satisfy or break the guard, the same reason
    the shell half strips ``#`` lines by hand.

    A literal containing ``-I`` IS an isolated interpreter spawn: ``-I`` implies
    ``-E``, so that child ignores ``PYTHONPYCACHEPREFIX``. That is the rule this
    encodes, rather than a list of files that goes stale the moment someone adds
    a probe.
    """
    root = Path(__file__).parent.parent / "src" / "kiro_crew"
    found: list[tuple[Path, int, list[str]]] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            flags = [
                el.value
                for el in node.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
            if "-I" in flags:
                found.append((path, node.lineno, flags))
    return found


def test_bytecode_policy_is_scoped_by_whether_the_prefix_can_reach_the_child():
    """Bytecode is REDIRECTED where the redirect works, and FORBIDDEN where it cannot.

    The desktop shell redirects the cache rather than forbidding it
    (``gateway-env.js``'s ``gatewayBytecodeEnvironment`` sets
    ``PYTHONPYCACHEPREFIX`` outside the bundle on POSIX and lets the packaged
    Windows bundle consume its build-time caches), which keeps warm-start
    caching. But an environment variable does not reach every child, so the
    policy has two complementary halves and this guard pins both:

    1. **Where the prefix reaches** — the bundled entry points and the gateway
       spawn that inherit it — there must be NO ``-B``. It is an interpreter
       flag no environment variable can lift, so one reappearing there would
       silently defeat the warm start for that path only.
    2. **Where the prefix CANNOT reach** — any child started with ``-I``, which
       implies ``-E`` and therefore ignores every ``PYTHON*`` variable — ``-B``
       is REQUIRED. There the redirect is inert and CPython falls back to its
       default location: beside the bundled sources inside a code-signed
       ``.app``. Those children are one-shot probes and helpers, so the cache
       they give up is worth nothing.

    So a new isolated spawn that forgets ``-B`` fails here, and a ``-B``
    sneaking back onto a prefix-reachable entry point fails here too.
    ``-s`` is a separate concern (no user site-packages) and must survive.
    """
    build_script = SCRIPT.read_text()
    electron = Path(__file__).parent.parent / "website" / "electron"
    supervisor = (electron / "gateway-supervisor.js").read_text()
    gateway_env = (electron / "gateway-env.js").read_text()

    # The owner exists and is what the spawn reads.
    assert "function gatewayBytecodeEnvironment(" in gateway_env
    assert "PYTHONPYCACHEPREFIX" in gateway_env
    assert "gatewayBytecodeEnvironment(" in supervisor
    # ... and only that helper names the variable, so there is no second writer.
    assert "PYTHONPYCACHEPREFIX" not in supervisor

    # --- Half 1: the prefix-reachable entry points keep -s and carry no -B. ---
    assert 'exec "$DIR/python3.12" -s -m kiro_crew "$@"' in build_script
    assert r'"%%~dp0..\\python.exe" -s -m kiro_crew %%*' in build_script
    assert 'spawnArgs = ["-s", "-m", "kiro_crew", ...spawnArgs]' in supervisor
    # Comment lines are stripped first: the launcher's own comment explains why -B
    # is absent, and matching that text would make the guard unfixable.
    shell_code = "\n".join(
        line for line in build_script.splitlines() if not line.lstrip().startswith("#")
    )
    assert " -B " not in shell_code, (
        "packaging/build-desktop.sh re-added the -B floor on a prefix-reachable "
        "entry point; PYTHONPYCACHEPREFIX cannot override an interpreter flag"
    )
    assert '"-B"' not in supervisor, (
        "gateway-supervisor.js re-added the -B floor; it would override the prefix"
    )

    # --- Half 2: every env-unreachable (-I) spawn carries -B. ---
    isolated = _argv_literals_with_isolation()
    # Non-vacuity: the known isolated spawns are the probe shim, the post-exec
    # spawn shim, the namespace launcher's flag tuple, the ReDoS pattern child,
    # the two interpreter-version probes, and the process-group supervisor.
    assert len(isolated) >= 7, f"the -I scan went vacuous, found {isolated!r}"
    modules = {path.name for path, _, _ in isolated}
    assert {
        "sandbox.py",
        "validation.py",
        "platform_compat.py",
        "dep_sync.py",
        "kiro_prerequisite.py",
    } <= modules, f"the -I scan stopped seeing known isolated spawns: {sorted(modules)}"
    missing = [
        f"{path.relative_to(path.parent.parent.parent)}:{lineno}"
        for path, lineno, flags in isolated
        if "-B" not in flags
    ]
    assert not missing, (
        "isolated (-I) interpreter spawns without -B: "
        + ", ".join(missing)
        + " — -I implies -E, so PYTHONPYCACHEPREFIX cannot reach these children "
        "and their bytecode would land inside the signed bundle"
    )
