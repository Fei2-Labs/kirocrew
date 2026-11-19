"""Repository discovery for the Git panel's status route.

``GET /api/project/git/status`` on a project directory that is not itself a
repository answers with the repositories BELOW it, grouped per repository.
:func:`_discover_repo_roots` finds them with a bounded, cached downward scan, and
:func:`_verify_descendant_roots` re-checks every cached root at use time, because a
root can be swapped for a link out of the project inside the cache window.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from itertools import islice

    from kiro_crew.dashboard.handlers.files import (
        _PROJECT_TREE_SKIP_DIRS,
        _REPO_SCAN_MAX_DEPTH,
        _REPO_SCAN_MAX_ENTRIES,
        _REPO_SCAN_MAX_REPOS,
        _REPO_SCAN_TTL_SECONDS,
        _is_not_a_repo_verdict,
        _probe_git_dir,
        _read_git_meta_prefix,
        _repo_scan_cache,
        _run_git_bounded,
        is_sensitive_path,
    )


def _discover_repo_roots(
    base: str, git_cmd: list[str], env: dict, fresh: bool = False
) -> tuple[list[str], bool, bool]:
    """Repo roots covering ``base``: its own repo if it has one, else its
    descendants' (sorted, so the grouped order is stable across polls).

    Returns the roots, whether a bound truncated the search, and whether the
    repository probe itself FAILED for an operational reason. The bound flag lets
    a caller say that repositories are missing rather than presenting a short
    list as the whole workspace; the failure flag is what keeps an outage from
    masquerading as an empty workspace.

    Only git's own not-a-repository verdict (:func:`_is_not_a_repo_verdict`, the
    same rule ``_run`` applies) licenses the downward scan: every other probe failure is an outage, and
    scanning downward from it would answer "no repository" for a repo we simply
    could not read -- the panel would then draw a clean workspace over it.

    ``fresh`` bypasses the TTL cache, so a manual refresh can pick up a repo
    cloned into the workspace instead of waiting out the poll cache.
    """
    now = time.monotonic()
    cached = _repo_scan_cache.get(base)
    if not fresh and cached is not None and now - cached[0] < _REPO_SCAN_TTL_SECONDS:
        return cached[1], cached[2], False

    roots: list[str] = []
    bounded = False
    probe_rc, probe_err = _probe_git_dir(base, env)
    if probe_rc != 0 and not _is_not_a_repo_verdict(probe_err):
        # Operational failure, NOT confirmed absence. Nothing is cached: the next
        # poll must re-probe rather than serve an outage as a settled answer.
        return [], False, True
    if probe_rc == 0:
        root_rc, root_out, _ = _run_git_bounded(
            [*git_cmd, "rev-parse", "--show-toplevel"], cwd=base, env=env, timeout=5
        )
        # git answers with forward slashes even on Windows, while `base` and the
        # scanned descendants carry native separators. Normalising here keeps every
        # root in one spelling, so the dispatch below can compare them as paths and
        # a row's `repoRoot` does not change shape with the discovery route.
        roots = [os.path.normpath(root_out.strip()) if root_rc == 0 else base]
    else:

        visited = 0

        def _scan(directory: str, depth: int) -> None:  # noqa: C901
            # Depth and repo count alone leave the walk unbounded over a merely
            # large tree, so cap the directories visited too: an over-budget scan
            # reports what it found rather than stat'ing the whole subtree every
            # time the cache expires.
            nonlocal visited, bounded
            if len(roots) >= _REPO_SCAN_MAX_REPOS or visited >= _REPO_SCAN_MAX_ENTRIES:
                bounded = True
                return
            if depth > _REPO_SCAN_MAX_DEPTH:
                bounded = True
                return
            try:
                # Pull at most the remaining budget (+1 to detect the overflow)
                # BEFORE sorting: `sorted(os.scandir(...))` materialises every
                # entry first, so one directory holding millions of them exhausts
                # memory no matter what the per-entry cap says afterwards.
                room = _REPO_SCAN_MAX_ENTRIES - visited
                with os.scandir(directory) as it:
                    batch = list(islice(it, room + 1))
            except OSError:
                # An unreadable directory is one branch of the walk, not the end
                # of it: raising here would surface as a 500 for the whole panel.
                bounded = True
                return
            if len(batch) > room:
                bounded = True
                batch = batch[:room]
            # Every entry pulled is charged, not only the ones worth descending
            # into, so a wide tree of skipped names cannot walk for free.
            visited += len(batch)
            entries = sorted(batch, key=lambda e: e.name)
            for entry in entries:
                if len(roots) >= _REPO_SCAN_MAX_REPOS:
                    bounded = True
                    return
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name in _PROJECT_TREE_SKIP_DIRS or entry.name.startswith("."):
                        continue
                    is_repo = os.path.exists(
                        os.path.join(entry.path, ".git")
                    ) and _git_dir_is_contained(entry.path, os.path.realpath(base))
                except OSError:
                    # Entry metadata can fail on its own (a vanished entry, a
                    # permission-denied mount point); skip that entry only.
                    bounded = True
                    continue
                if is_repo:
                    # A submodule's status belongs to its parent.
                    roots.append(entry.path)
                    continue
                _scan(entry.path, depth + 1)

        _scan(base, 1)

    _repo_scan_cache[base] = (now, roots, bounded)
    return roots, bounded, False


def _git_pointer_gitdir(root: str) -> str | None:
    """Resolve the ``gitdir: <path>`` pointer a linked worktree writes as ``.git``.

    Returns the (possibly relative-resolved) git directory, or None when ``.git``
    is not a readable pointer file.
    """
    pointer = _read_git_meta_prefix(os.path.join(root, ".git"))
    if pointer is None or not pointer.startswith("gitdir:"):
        return None
    gitdir = pointer.split(":", 1)[1].strip()
    if not gitdir:
        return None
    return gitdir if os.path.isabs(gitdir) else os.path.join(root, gitdir)


def _git_dir_is_contained(root: str, real_base: str) -> bool:
    """Does the repo at ``root`` keep its git directory inside ``real_base``?

    Both spellings of ``<root>/.git`` can name a directory outside the project: a
    linked worktree writes a FILE holding ``gitdir: <path>``, and a directory entry
    can itself be an indirection (symlink, or a Windows junction, which
    ``os.path.islink`` does not report). Git then reads the OTHER repository's
    metadata and, because its tracked files are absent here, reports its whole file
    list as deletions.

    So the form is never the test: whatever ``.git`` is, the git directory it
    resolves to must land under ``real_base``. ``realpath`` collapses every
    indirection, which is what keeps a newly-invented link flavour from needing a
    predicate of its own.
    """
    dot = os.path.join(root, ".git")
    try:
        resolved = os.path.realpath(dot)
        is_dir = os.path.isdir(resolved)
    except OSError:
        return False
    if not is_dir:
        gitdir = _git_pointer_gitdir(root)
        if gitdir is None:
            return False
        resolved = os.path.realpath(gitdir)
    return resolved == real_base or resolved.startswith(real_base + os.sep)


def _verify_descendant_roots(base: str, roots: list[str]) -> tuple[list[tuple[str, str]], bool]:
    """Keep only roots that are still real directories contained in ``base``.

    Discovery refuses a symlinked entry, but its result is cached, so this runs at
    USE time: a child swapped for a symlink out of the project within the TTL
    would otherwise have git run in it, disclosing another repository's branch and
    paths through an endpoint scoped to the project. Returns each surviving root
    paired with the realpath this call APPROVED -- the git spawn compares against
    that stored identity, since re-resolving the path later would follow a swap
    made after this check and agree with it.
    """
    real_base = os.path.realpath(base)
    kept: list[tuple[str, str]] = []
    dropped = False
    for root in roots:
        real = os.path.realpath(root)
        contained = real == real_base or real.startswith(real_base + os.sep)
        if os.path.islink(root) or not contained or is_sensitive_path(real):
            dropped = True
            continue
        try:
            if not os.path.isdir(real):
                dropped = True
                continue
        except OSError:
            dropped = True
            continue
        # Re-checked here and not only at scan time: the pointer file can be
        # rewritten inside the cache TTL, which is the same window the root
        # containment check above exists to close.
        if not _git_dir_is_contained(root, real_base):
            dropped = True
            continue
        kept.append((root, real))
    return kept, dropped
