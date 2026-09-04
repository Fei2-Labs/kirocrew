"""Fork identity: upstream's base version plus a git-derived fork revision.

This repository is a long-lived FORK. Its ``__version__`` is deliberately
upstream's own literal (``0.4.0-rc.9``), because every piece of release
machinery compares against it: the changelog gate, the promotion contract, the
release feed's ``_is_newer``, the desktop auto-updater's compare gate. Rewriting
it to a fork-private number would decouple all of those from the artifacts they
describe.

The consequence is that a fork build is INDISTINGUISHABLE from the upstream
build it was forked from — an upstream install stamped ``0.4.1-insider.1`` even
reads as newer. So the fork half is carried in a PEP 440 **local version
segment** appended for display and identity only::

    0.4.0-rc.9+fork.g645d7289          a clean fork build
    0.4.0-rc.9+fork.g645d7289.dirty    uncommitted changes in the working tree
    0.4.0-rc.9                         no revision could be derived

Three properties make the local segment the right carrier:

* It is legal PEP 440 and legal SemVer build metadata, so nothing downstream
  chokes on it.
* PEP 440 IGNORES it for ordering, so it can never make a build compare as a
  different release — provided every comparison strips it first, which is what
  :func:`strip_local` exists for.
* It is derived from git, so it never has to be hand-bumped and it cannot drift
  from the commit it names.

WHERE THE REVISION COMES FROM, in priority order:

1. ``_build_info.FORK_REVISION`` / ``FORK_DIRTY``, baked by
   ``scripts/stamp-distribution.sh`` at packaging time. A wheel, dmg, deb, rpm,
   or container has NO ``.git``, so build time is the only moment the answer
   exists. ``docs/build/release.md`` requires anything a stable user will see to
   be baked into the RC bytes before the RC is cut; promotion never rebuilds, so
   a runtime derivation would answer differently on the promoted artifact.
2. A live ``git`` call, for a source checkout. Lazy and memoized per process,
   and NEVER at import time — a subprocess at module import would run in every
   CLI invocation and every test collection, whether or not anyone asked for a
   version. Two spawns with a five-second timeout each also cannot run on the
   event loop, so the derivation is SPLIT: :func:`warm` derives (called from the
   update check's existing off-loop step) and :func:`peek_revision` only reads
   the memo, for the ``/api/status`` payload and the 5-second WebSocket push.
   A one-shot command like ``kirocrew --version`` derives directly; it is not on
   a loop and there is nothing to warm it.
3. ``""`` — an installed package with neither a baked stamp nor a repository.
   The version then renders as the plain base, which is honest. A placeholder
   like ``+fork.unknown`` would be worse than silence: it asserts a fork build
   on an install that may well be upstream's own wheel.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading

from kiro_crew import __version__

logger = logging.getLogger(__name__)

#: PEP 440's local-version separator. Everything from the FIRST occurrence is
#: the local segment.
LOCAL_SEPARATOR = "+"

#: The local segment's leading identifier, so a reader can tell a fork build
#: from any other local segment (a distro rebuild, a ``pip install .`` from a
#: patched tree) at a glance.
FORK_LABEL = "fork"

#: Prefix on the revision identifier, mirroring ``git describe``'s ``g<sha>``.
#: A purely numeric local identifier would be legal but unreadable, and SemVer
#: build metadata forbids leading zeros in a numeric identifier — which a hex
#: sha hits roughly one time in sixteen.
REVISION_PREFIX = "g"

#: Marks a build made from a working tree with uncommitted changes. Without it a
#: local build is indistinguishable from the clean commit it was built near, and
#: a bug report then names a commit whose contents were never what ran.
DIRTY_IDENTIFIER = "dirty"

#: Short-sha width. Eight is git's own default for a repository of this size and
#: is unambiguous well past a million objects.
REVISION_WIDTH = 8

#: Ceiling on the git probe. Long enough for a cold index on a slow disk, short
#: enough that a stale network mount cannot hang ``kirocrew --version``.
_GIT_TIMEOUT_SECONDS = 5

#: Where an UNTRACKED file could still change what this build runs: the Python
#: package, and the frontend sources that are built into ``static/dist`` and
#: served from it. Untracked entries anywhere else are operator scratch and do
#: not make a build dirty -- see the reasoning in ``_git_revision``.
SHIPPED_SOURCE_PATHS = ("src/kiro_crew", "website/src")

#: A short or full lowercase-hex object name. Applied to git's own output as
#: well as to a baked value: the baked module is generated, but it is generated
#: from a shell variable, and an unvalidated value would put arbitrary text in
#: the middle of a version string that a dozen regexes then parse.
_REVISION_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")

#: Environment variables that point git at a DIFFERENT repository than ``-C``
#: names, so the probe cannot be made to describe an unrelated tree. Same list
#: and same reason as ``platform.update_capability._GIT_LOCATION_ENV``.
_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)

#: Sentinel distinct from ``("", False)``: a derivation that RAN and found
#: nothing is cached, so a non-repository install does not re-spawn git on every
#: call. ``None`` means "not derived yet".
_cached: tuple[str, bool] | None = None
_cache_lock = threading.Lock()


def strip_local(version: str) -> str:
    """Return *version* without its PEP 440 local segment.

    EVERY caller that classifies, parses, or compares a version string must go
    through here first. The trap this closes is specific and it fails silently:

    ``release_channel._PEP440_PRERELEASE`` is an UNANCHORED ``(?:a|b|rc)\\d+``,
    and a git sha is hex — so ``a`` or ``b`` followed by a decimal digit occurs
    in roughly 46% of eight-character shas (measured, not estimated). A fork
    build of a STABLE base then classifies as ``insider`` **depending on the
    commit hash**::

        0.5.0+fork.g645d7289   ->  stable    (no a/b + digit in the sha)
        0.5.0+fork.gb1234ab    ->  insider   WRONG, from the sha's "b1234"
        0.5.0+fork.ga1b2c3d    ->  insider   WRONG, from the sha's "a1"

    That feeds the bug-report channel label, ``is_prerelease()``, and the
    dashboard — intermittently, and only for some commits, which is the worst
    shape a bug can have.

    Ordering is the second reason. PEP 440 ignores the local segment when
    ordering, but the comparators here do not parse it: ``_version_key`` falls
    through to its semver branch, reads the FIRST integer out of
    ``fork.gb1234ab``, and ranks the build as prerelease ``1234`` of its base —
    below the bare release. A fork build of ``0.5.0`` is then offered upstream's
    ``0.5.0`` as an "update" that would replace the fork.

    A string with no local segment is returned unchanged, so this is safe to
    apply unconditionally.
    """
    return str(version or "").split(LOCAL_SEPARATOR, 1)[0].strip()


def _baked_revision() -> tuple[str, bool] | None:
    """The revision stamped into the package tree, or ``None`` if unstamped.

    ``ImportError`` is the NORMAL case in a git checkout — ``_build_info.py`` is
    generated at packaging time and is gitignored — so it is not logged.
    """
    try:
        from kiro_crew import _build_info
    except ImportError:
        return None
    # ``FORK_STAMPED`` is what makes the baked answer TERMINAL, including when it
    # is empty. Without it, a packaging run on a host with no git would bake an
    # empty revision, this would report "no baked answer", and every packaged
    # install would then spawn git once looking for a repository it does not
    # have. Wheels released before this field existed lack it and correctly fall
    # through.
    if not getattr(_build_info, "FORK_STAMPED", False):
        return None
    dirty = getattr(_build_info, "FORK_DIRTY", False) is True
    revision = str(getattr(_build_info, "FORK_REVISION", "") or "").strip().lower()
    if not revision:
        # Stamped by an upstream-shaped build, or by a packaging host with no
        # git. Both mean "no fork revision", authoritatively.
        return "", False
    if not _REVISION_RE.fullmatch(revision):
        # A stamped-but-unusable value is a packaging bug, not a normal state:
        # report it rather than put arbitrary text inside a version string that
        # a dozen regexes downstream then parse.
        logger.warning("Baked FORK_REVISION is not an object name; ignoring it")
        return "", False
    return revision[:REVISION_WIDTH], dirty


def _repo_root() -> str:
    """Where to point git. The package's parent tree, not the process CWD.

    CWD is wrong for both of the callers that matter: the gateway runs from
    wherever it was launched, and pytest runs from the repo root of whatever
    checkout collected it. ``__file__`` is the only path that names THIS code.
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git_revision() -> tuple[str, bool]:
    """Derive ``(short_sha, dirty)`` from git, or ``("", False)``.

    Degrades silently to ``("", False)`` on every failure — no git binary, not a
    repository, a timeout, an undecodable answer. A version string is not worth
    an exception, and a source tree that is not a checkout (an unpacked sdist)
    is an ordinary state rather than an error.
    """
    # Local import: platform_compat is a large module and this path runs at most
    # once per process, on a display surface.
    from kiro_crew import platform_compat

    git_bin = platform_compat.trusted_git_bin()
    if git_bin is None:
        # `None` is a REFUSAL, not "try a bare git": PATH can legitimately lead
        # with agent-writable directories, and a planted shim would run with the
        # gateway's environment.
        return "", False
    root = _repo_root()
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}

    def _run(args: list[str]) -> str | None:
        try:
            proc = subprocess.run(
                [git_bin, "-C", root, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT_SECONDS,
                env=env,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    head = _run(["rev-parse", f"--short={REVISION_WIDTH}", "HEAD"])
    if head is None:
        return "", False
    revision = head.strip().lower()
    if not _REVISION_RE.fullmatch(revision):
        return "", False

    # Dirtiness is a SEPARATE call rather than `describe --dirty`, because this
    # fork does not tag: `describe` without a reachable tag fails outright, and
    # `describe --always --dirty` answers with the sha but is the same two walks
    # underneath.
    #
    # Untracked files are counted only under the paths that actually SHIP. A
    # bare `status --porcelain` reads every untracked entry as dirtiness, and
    # this repo permanently carries operator scratch (`.kiro/`, `.trellis/`,
    # `.playwright-mcp/`) that is untracked without being ignored — so the flag
    # stuck on for every build on a working checkout, and a marker that never
    # clears carries no signal. An untracked file under `src/kiro_crew/` or
    # `website/src/` can change what runs; one beside them cannot.
    status = _run(["status", "--porcelain", "--untracked-files=no"])
    dirty = bool(status is not None and status.strip())
    if not dirty:
        shipped = _run(["status", "--porcelain", "--", *SHIPPED_SOURCE_PATHS])
        dirty = bool(shipped is not None and shipped.strip())
    return revision[:REVISION_WIDTH], dirty


def fork_revision() -> tuple[str, bool]:
    """``(short_sha, dirty)`` for this build, or ``("", False)`` if unknowable.

    Cached for the life of the process, including the negative answer, so a
    non-repository install does not re-probe on every call.

    MAY SPAWN GIT, so it is for callers with no event loop under them —
    ``--version``, the gateway banner, a bug report. An on-loop caller uses
    :func:`peek_revision`, and :func:`warm` is the off-loop derivation that
    fills the memo for it.
    """
    global _cached
    if _cached is not None:
        return _cached
    with _cache_lock:
        # Re-check under the lock: two threads can pass the fast path together.
        if _cached is None:
            _cached = _baked_revision() or _git_revision()
        return _cached


def peek_revision() -> tuple[str, bool] | None:
    """The memoized ``(short_sha, dirty)``, or ``None`` if not derived yet.

    NEVER derives. Exists for the callers that run ON THE EVENT LOOP — the
    ``/api/status`` payload and the 5-second WebSocket push. :func:`fork_revision`
    may spawn two git processes with a five-second timeout each, which on the
    loop is a multi-second stall of every session; and the loop is exactly where
    an unwarmed first call would land, because the status payload is usually the
    first thing to ask.

    ``None`` means "not known yet", and such a caller renders no fork
    attribution rather than blocking to find out. :func:`warm` is what fills it,
    from the update check's existing off-loop step.
    """
    return _cached


def warm() -> tuple[str, bool]:
    """Derive and memoize the revision. MUST be called off the event loop.

    Thin alias for :func:`fork_revision`, named for its one job so a reader of
    the call site can see that the spawn is deliberate and that the point is the
    side effect on the cache.
    """
    return fork_revision()


def local_segment() -> str:
    """The ``+fork.g<sha>[.dirty]`` suffix, or ``""`` when no revision is known."""
    revision, dirty = fork_revision()
    if not revision:
        return ""
    parts = [FORK_LABEL, f"{REVISION_PREFIX}{revision}"]
    if dirty:
        parts.append(DIRTY_IDENTIFIER)
    return LOCAL_SEPARATOR + ".".join(parts)


def is_fork_build() -> bool:
    """Whether a fork revision could be derived for this build.

    False for an installed package with no baked stamp and no repository, which
    is deliberately the same answer an upstream wheel gives: this cannot prove a
    fork it has no evidence of.
    """
    return bool(local_segment())


def base_version() -> str:
    """Upstream's declared version — ``__version__`` with any local segment off.

    The value every comparison, every packaging manifest, and the changelog gate
    use. Identical to ``__version__`` today; routed through :func:`strip_local`
    so that stays true if the literal ever grows a local segment.
    """
    return strip_local(__version__)


def full_version(base: str | None = None) -> str:
    """The build's full identity: base plus fork local segment.

    DISPLAY AND IDENTITY ONLY — ``--version``, the About surface, a bug report.
    Never a comparison input; use :func:`base_version` for those.

    *base* exists so a caller passes ITS OWN module binding of ``__version__``
    rather than having this function reach for the package's. Several callers
    (``diagnostics``, ``cli_server``) are tested by monkeypatching that binding,
    and a function that read the canonical one would silently ignore the patch —
    reporting the real build's version inside a test that thinks it pinned
    another. Any local segment on *base* is stripped, so passing an
    already-composed version is idempotent.
    """
    return strip_local(base if base is not None else __version__) + local_segment()


def reset_cache_for_tests() -> None:
    """Drop the memoized revision. Tests only."""
    global _cached
    with _cache_lock:
        _cached = None


__all__ = [
    "DIRTY_IDENTIFIER",
    "FORK_LABEL",
    "LOCAL_SEPARATOR",
    "REVISION_PREFIX",
    "REVISION_WIDTH",
    "base_version",
    "fork_revision",
    "peek_revision",
    "warm",
    "full_version",
    "is_fork_build",
    "local_segment",
    "reset_cache_for_tests",
    "strip_local",
]
