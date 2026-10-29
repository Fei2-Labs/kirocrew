"""The mise Node runtime ladder for adapter discovery.

Fork-owned. ``shutil.which`` returns only the FIRST hit for an adapter name, and
on a host where mise's shim dir precedes its version dirs that first hit is the
shim — resolving at spawn time to whatever node mise happens to have active,
which can be an EOL version even when a supported install sits later on the same
PATH. The helpers here order and vet the candidates so the shared spawn ladder
(:mod:`kiro_crew.acp.launch`) pairs an adapter with a runtime it can actually
run on. The installs-dir location itself is shared with launch and imported from
there, so ``MISE_DATA_DIR`` / ``XDG_DATA_HOME`` relocation is answered in one
place.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.acp import launch as _launch

#: Oldest Node major an ACP adapter bundle is allowed to be paired with.
#: Measured, not guessed: codex-acp 1.4.0 dies on Node 16 with ``TypeError:
#: Writable.toWeb is not a function`` — that API arrived in Node 17 — and returns
#: no ACP response at all. The adapters declare no ``engines``, so npm installs
#: them happily under an EOL Node and nothing downstream objects. 18 rather than
#: 17 because 17 was never an LTS line and no adapter targets it.
_MIN_ADAPTER_NODE_MAJOR = 18


def _node_major_is_supported(version_dir_name: str) -> bool:
    """Is this mise version directory a Node new enough to run an adapter?

    mise install dirs are named by version (``24.18.0``, ``24``, ``16``), so the
    leading integer is the major. An UNPARSEABLE name returns True: the floor
    exists to reject a runtime measured as broken, not to gate on a naming
    convention this function does not own, and failing closed here would refuse
    a perfectly good install whose directory is spelled unusually.
    """
    head = version_dir_name.lstrip("v").split(".", 1)[0]
    try:
        return int(head) >= _MIN_ADAPTER_NODE_MAJOR
    except ValueError:
        return True


def _script_runtime_is_supported(script_path: str) -> bool:
    """Is this candidate outside a too-old mise Node tree?

    Checking the paired node is not enough. A candidate under an EOL tree is
    usually ALSO an executable shim whose shebang resolves to that same EOL node,
    so a ladder that only rejects the pairing falls through to "run the script
    directly" and re-acquires the runtime it just refused. Measured: rejecting
    only the pairing turned a Node 16 ``[node, index.js]`` argv into a Node 16
    ``[codex-acp]`` argv, which crashes identically.

    A script outside mise's installs dir is not our business and passes.
    """
    try:
        resolved = Path(script_path).resolve()
        mise_installs = _launch._mise_node_installs_dir().resolve()
        rel = resolved.relative_to(mise_installs)
    except (ValueError, IndexError, OSError):
        return True
    if not rel.parts:
        return True
    return _node_major_is_supported(rel.parts[0])


def _is_mise_shim(script_path: str) -> bool:
    """Does this path go through mise's version-agnostic shim dir?

    A shim resolves to mise's ACTIVE node at spawn time, which depends on global
    config and cwd and can be an EOL version even when a supported one is
    installed. For an adapter that must be paired with a known-good runtime, a
    shim is a last resort, never a preference.
    """
    try:
        resolved = Path(script_path).resolve()
    except OSError:
        return False
    return "shims" in resolved.parts and "mise" in str(resolved)


def _under_mise_installs(script_path: str) -> bool:
    """Is this path inside a mise Node version tree (not a shim)?"""
    try:
        Path(script_path).resolve().relative_to(_launch._mise_node_installs_dir().resolve())
    except (ValueError, OSError):
        return False
    return True


def _ordered_path_matches(name: str, search_path: str) -> list[str]:
    """Every match for *name* on *search_path*, best runtime first.

    ``shutil.which`` returns only the FIRST hit, and on a host where mise's shim
    dir precedes its version dirs that first hit is the shim — so the supported
    install sitting later on the same PATH is never considered. Ordering here
    puts concrete supported installs ahead of shims and unsupported trees.
    """
    found: list[str] = []
    for entry in search_path.split(os.pathsep):
        if not entry:
            continue
        candidates = [Path(entry) / name]
        # Windows resolves extensionless command names through PATHEXT. Keep
        # the bare path too: a non-executable Node entry script may still be
        # runnable through its interpreter on POSIX.
        which_match = shutil.which(name, path=entry)
        if which_match:
            candidates.append(Path(which_match))
        for candidate in candidates:
            if platform_compat.is_executable_file(candidate) or candidate.is_file():
                found.append(str(candidate))

    def rank(p: str) -> tuple[int, int]:
        # 0 = concrete supported tree, 1 = outside mise, 2 = shim, 3 = too old.
        if not _script_runtime_is_supported(p):
            return (3, 0)
        if _is_mise_shim(p):
            return (2, 0)
        return (0, 0) if _under_mise_installs(p) else (1, 0)

    return sorted(dict.fromkeys(found), key=rank)
