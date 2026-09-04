#!/usr/bin/env python3
"""ratchet_scope.py -- one answer to "which files and lines does THIS change touch".

The merge-ref ratchets (``check_black_formatting.py``,
``check_subprocess_encoding.py``, ``check_agent_sdk_boundary.py``,
``check_sync_io_in_async.py``) record a pre-existing violation set as legacy and
then judge only what the change in front of them adds. All four need the same pair
of answers -- the changed-file set and the added-line set -- and they must agree:
a scope fix applied to one private copy and not the others would make the same
added line red under one gate and green under another, for no reason a
contributor could see.

Two gate families share this module and differ ONLY in how the base is named.
The merge-ref family above discovers the checkout shape itself
(``changed_paths()`` / ``added_lines()``). ``check_brand_name.py``,
``check_harness_parity.py`` and ``check_focus_cue.py`` are handed an explicit
base ref instead (``*_BASE_REF``, resolved inside Actions to the PR's
``base.sha`` so a run never picks up base moves landing after it started); they
use the ``*_at`` entry points below (``resolve_base`` / ``changed_paths_at`` /
``added_lines_at``), which keep that env-provided base semantics while sharing
the diff PARSING — N private parsers meant the same added line could be judged
differently by different gates, and a scope fix to one copy left the others
wrong.

A merge commit has TWO ancestries, and both answers have to account for that:
content that already existed in either one was not written by the change in
front of the gate. ``imported_parents`` / ``scope_bases`` name those ancestries,
``added_lines`` drops any line whose text one of them already carried in that
file, and a count-baseline gate reads ``scope_bases`` for the same correction on
its per-file counts. Without it a fork
sync reddens every gate for thousands of upstream-authored lines, and the only
remedy left is the baseline raise each ratchet header forbids.

``changed_paths`` names each checkout shape it tries and reports the winner,
because they fail in ways that look alike and an earlier version of the black
gate silently fell back to whole-tree scope on CI. ``added_lines`` reads the diff
endpoints that SAME answer named, so both descriptions are of one diff -- an
added-line set computed against a different base than the changed-file set is
worse than no added-line set at all.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Set to a non-empty value to make :func:`changed_paths` answer "whole tree".
#:
#: A diff scope is the right question on a pull request and the WRONG one on a
#: push to a branch that is already the base: there ``origin/main...HEAD`` is
#: empty, so every consuming ratchet judges zero files and reports green
#: whatever the tree holds. A caller that means to measure an integrated tree
#: says so here rather than relying on a diff that happens to be empty.
WHOLE_TREE_ENV = "RATCHET_SCOPE_WHOLE_TREE"

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout


def _git_strict(*args: str) -> str:
    """Run git, returning stdout and RAISING ``CalledProcessError`` on failure.

    The explicit-base entry points use this instead of :func:`_git` because
    their callers fail CLOSED: an env-base gate that cannot see its base must
    refuse to pass, and the exception carries git's stderr so each gate can
    fold it into its own gate-named message. ``errors="replace"`` for the same
    reason as everywhere else here: ``--text`` makes git emit the content of a
    file that is not valid UTF-8, and a strict decode would raise inside
    ``subprocess`` — a traceback instead of a verdict.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout


def _first_parent_is_base() -> bool:
    """True when HEAD's FIRST parent is the base branch's commit.

    Two checkout shapes answer "HEAD is a merge", and they need opposite diffs:

    * CI's ``pull_request`` merge ref puts the BASE first, so ``HEAD^1..HEAD``
      is exactly this change.
    * A local ``git merge origin/main`` on a feature branch puts the FEATURE
      tip first, so ``HEAD^1..HEAD`` is only what main brought in and the
      branch's own commits are invisible -- every consuming ratchet then
      under-scopes, and a local run passes a gate CI goes on to fail.

    Which shape this is is decided by which parent the base branch can reach:
    on the CI shape ``HEAD^1`` IS a commit of the base. The base refs tried
    here are the same pair, in the same order, as the three-dot fallback's, so
    a checkout with only a local ``main`` (a merge made ON main has its prior
    tip as ``HEAD^1``, which ``main`` reaches) still recognises the shape. A
    failing git -- neither ref resolvable -- answers False, so an
    unrecognisable merge falls through to the three-dot attempts rather than
    trusting a parent order nothing verified.
    """
    for base in ("origin/main", "main"):
        code, _ = _git("merge-base", "--is-ancestor", "HEAD^1", base)
        if code == 0:
            return True
    return False


def changed_paths() -> tuple[set[str] | None, str]:
    """This change's paths plus how they were determined, for the log.

    Several checkout shapes have to work and they fail in ways that look alike, so
    each attempt is named and the winner is printed. Guessing silently is what let
    an earlier version of the black gate fall back to whole-tree scope on CI
    without saying so, and then report a file the base branch had merged.

    * A ``pull_request`` checkout leaves HEAD as the MERGE commit, whose tree is
      the base tree plus this change. So ``diff HEAD^1 HEAD`` is exactly this
      change -- but only once ``_first_parent_is_base`` confirms, against a
      resolvable base ref, that ``HEAD^1`` really is the base: a local
      ``git merge origin/main`` is ALSO a merge at HEAD, with the parents the
      other way around, and taking ``HEAD^1..HEAD`` there would scope to what
      main brought in instead of this change.
    * ``diff HEAD^1 HEAD^2`` is equivalent but needs BOTH parents' trees, which a
      shallow clone may not have.
    * Locally HEAD is the branch tip -- or an unrecognised merge -- so the
      three-dot diff against the base branch is the right question.

    None means undeterminable, and the caller must then judge the whole tree
    rather than nothing: a scope that fails open disables the gate exactly when
    its inputs are unusual.

    ``WHOLE_TREE_ENV`` short-circuits to that same None, for a caller whose
    question really is about a whole tree. It is checked FIRST so the answer
    cannot depend on which checkout shape a run happens to have -- an empty
    diff is indistinguishable from a clean change, and that ambiguity is what
    the override exists to remove.
    """
    if os.environ.get(WHOLE_TREE_ENV, "").strip():
        return None, f"whole tree ({WHOLE_TREE_ENV} set)"
    code, out = _git("rev-list", "--parents", "-n", "1", "HEAD")
    is_merge = code == 0 and len(out.split()) >= 3
    attempts: list[tuple[str, list[str]]] = []
    if is_merge and _first_parent_is_base():
        # No second endpoint, so the diff runs to the WORKING TREE. On CI's
        # merge ref the tree is clean and this is identical to `HEAD^1 HEAD`;
        # locally it is the difference between judging the commits and judging
        # what the gate actually scans, and naming HEAD while scanning the tree
        # puts a violation's line numbers and the added-line set in different
        # files. Under-scoping is the direction that lets a local run pass what
        # CI then fails, so the tree wins.
        attempts.append(("merge HEAD^1..HEAD", ["diff", "--name-only", "HEAD^1"]))
        attempts.append(("merge parents", ["diff", "--name-only", "HEAD^1", "HEAD^2"]))
    for base in ("origin/main", "main"):
        # `{base}...HEAD` is `merge-base(base, HEAD)..HEAD`, so the base is
        # resolved explicitly and the diff left with ONE endpoint -- it then
        # runs to the working tree, which is what every consuming gate scans.
        # `resolve_base` also survives a shallow clone with no shared history,
        # where the three-dot form simply fails.
        attempts.append((f"{base}...HEAD", ["diff", "--name-only", resolve_base(base)]))
    for label, args in attempts:
        code, out = _git(*args)
        if code == 0:
            return {line.strip() for line in out.splitlines() if line.strip()}, label
    return None, "undeterminable (judging the whole tree)"


def _added_from_diff(args: list[str]) -> dict[str, set[int]] | None:
    """Parse one whole-diff ``--unified=0`` invocation into added line numbers."""
    proc = subprocess.run(
        ["git", "--no-pager", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return None
    added: dict[str, set[int]] = {}
    current: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+++ "):
            current = None  # /dev/null or unusual prefix
        elif current is not None:
            match = _HUNK_RE.match(line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) is not None else 1
                added.setdefault(current, set()).update(range(start, start + count))
    return added


def _scope_endpoints(scope_label: str) -> tuple[str, str, str | None] | None:
    """``(base commit, tip commit, where the tip's CONTENT is)`` for a scope label.

    One place decides what a label's two endpoints actually are, because three
    separate answers depend on it and they must agree: the changed-file set, the
    added-line set, and the ancestry walk. The third element is where the tip's
    text is READ from -- ``None`` meaning the working tree, which is the tip of
    every scope whose diff has no second endpoint. That matters because the gates
    scan the working tree: a line number produced against HEAD and looked up in
    the tree is a line number in the wrong file.

    ``None`` for an undeterminable scope, where the caller judges the whole tree
    and no correction applies.
    """
    if scope_label == "merge HEAD^1..HEAD":
        # Both endpoints RESOLVED: the ancestry walk matches the tip against
        # `rev-list` output, which prints full hashes, so a bare "HEAD" here
        # matches nothing and silently reports that nothing was imported.
        base, tip = _rev("HEAD^1"), _rev("HEAD")
        return (base, tip, None) if base and tip else None
    if scope_label == "merge parents":
        # The one label whose tip is not HEAD, so the working tree is neither
        # endpoint and the content comes from the commit.
        base, tip = _rev("HEAD^1"), _rev("HEAD^2")
        return (base, tip, tip) if base and tip else None
    if scope_label.endswith("...HEAD"):
        tip = _rev("HEAD")
        base = _rev(resolve_base(scope_label[: -len("...HEAD")]))
        return (base, tip, None) if base and tip else None
    return None


def _rev(ref: str) -> str | None:
    code, out = _git("rev-parse", "--verify", "--quiet", ref)
    resolved = out.strip()
    return resolved if code == 0 and resolved else None


def imported_parents(scope_label: str) -> list[str]:
    """Ancestries the scope's RANGE imported, which this change did not write.

    A merge commit has two ancestries, and a line that already existed in either
    one was not introduced by the person who made the merge. Judging such a line
    as added is not a stricter gate, it is a wrong answer: on a fork sync it
    reddens thousands of lines whose author is upstream, with no fix available in
    the merger's own diff, and the only way out is the baseline raise every
    ratchet header forbids.

    The parents split into two kinds, and telling them apart is the whole
    problem, because ``changed_paths`` reaches the merge shape from two very
    different checkouts:

    * CI's ``pull_request`` merge ref: ``HEAD^1`` is the base and ``HEAD^2`` is
      the PR head, which is THIS CHANGE. Treating it as pre-existing history
      would accept every line the PR adds -- the gate deleted, not fixed.
    * a real merge on the branch (``git merge upstream/main``): ``HEAD^1`` is the
      work so far and ``HEAD^2`` is foreign history being imported.

    **The tip's own shape is irrelevant.** An earlier version of this keyed on
    "HEAD is a merge", and that is a property of where the branch happens to
    stand: one ordinary commit on top of a fork-sync merge makes HEAD
    single-parent, the whole imported diff is attributed to the change again, and
    the same false verdict returns by a different door. A fast-forwardable PR is
    the same defect from the other side, because GitHub then hands CI a
    non-merge tip. So the question is asked of the RANGE: any merge between the
    base and the tip may have imported an ancestry, wherever it sits.

    **Range membership is not the criterion; the FIRST-PARENT chain is.** Asking
    only for parents outside ``base..tip`` sounds equivalent and is not: an
    imported upstream commit is reachable from the tip and unreachable from the
    base, so it is squarely inside the range and that test answers "nothing was
    imported". What separates the two is which side of a merge the range's own
    development went down. This change's own history is the first-parent chain
    from the tip; everything a merge on that chain joined from the other side is
    an ancestry the range imported rather than authored. The chain is also what
    keeps the walk cheap and bounded -- it stops at the base rather than at the
    root.

    Topology alone cannot answer one case, and it is the expensive one. A PR
    branch and an upstream branch both merely SHARE a fork point with the base,
    so on GitHub's synthetic ``pull_request`` merge ref the second parent -- the
    PR itself -- looks exactly like imported history, and accepting it would
    accept every line the PR adds. That ref is therefore recognised for what it
    is (:func:`_tip_is_pr_merge_ref`) and its other parents are seeded as own
    history, so the walk descends THROUGH the PR and still names the upstream
    ancestry a fork-sync merge inside it imported.

    A parent the base already contains is not imported either: a
    ``git merge origin/main`` into the branch brought in nothing the base lacked.
    An empty list whenever the range holds no merge at all, so an ordinary
    fast-forward change inherits nothing.
    """
    endpoints = _scope_endpoints(scope_label)
    if endpoints is None:
        return []
    base, tip, _content = endpoints
    # One call for the whole range's topology. Per-commit `rev-list -n 1` was
    # affordable only while the walk was three commits long; bounded by the
    # range, it is still the walk's entire cost.
    code, out = _git("rev-list", "--parents", f"{base}..{tip}")
    if code != 0:
        return []
    parents_of: dict[str, list[str]] = {}
    for line in out.splitlines():
        fields = line.split()
        if fields:
            parents_of[fields[0]] = fields[1:]

    own: set[str] = set()
    pending = [tip]
    if _tip_is_pr_merge_ref():
        # The merge ref's other parents are the change, not history it imported.
        pending.extend(parents_of.get(tip, [])[1:])
    while pending:
        commit: str | None = pending.pop()
        # Leaving the range means the base's own history: stop rather than walk
        # to the root.
        while commit is not None and commit in parents_of and commit not in own:
            own.add(commit)
            chain = parents_of[commit]
            commit = chain[0] if chain else None

    imported: list[str] = []
    for commit in sorted(own):
        for parent in parents_of.get(commit, [])[1:]:
            if parent in own or parent in imported:
                continue
            if _git("merge-base", "--is-ancestor", parent, base)[0] == 0:
                continue  # the base already contains it
            imported.append(parent)
    return imported


def _tip_is_pr_merge_ref() -> bool:
    """True when HEAD may be GitHub's SYNTHETIC ``pull_request`` merge commit.

    A True answer only ever moves HEAD's own non-first parents from "imported"
    to "own history", so it is inert when HEAD is not a merge -- which is why it
    can be asked unconditionally.

    This has to be answered from outside git, because inside git it is not
    answerable: the merge ref and a real ``git merge`` of a fork's upstream have
    the same shape, the same parent order, and the same fork-point relationship
    to the base. Getting it wrong in the merge-ref direction is the expensive
    one -- the second parent there is the PR under review, and calling it
    pre-existing history accepts every line the PR adds.

    Two independent signals, either of which is enough, and both of which hold
    on a real Actions run:

    * ``GITHUB_BASE_REF`` is set only for a ``pull_request`` event, which is the
      only event that produces a merge ref. It is the platform stating the shape
      rather than a guess about it.
    * the merge ref is checked out DETACHED, so no branch contains it. A real
      merge is made on a branch, which is what puts it on one.

    Answering True when unsure is the strict direction -- the change is then
    judged against the first parent alone, exactly as before this correction --
    so a git failure falls that way too.
    """
    if os.environ.get("GITHUB_BASE_REF"):
        return True
    # ``for-each-ref``, not ``branch --contains``: the latter prints a
    # ``(HEAD detached from ...)`` pseudo-entry for exactly the checkout this
    # question is about, which reads as "a branch contains it" and inverts the
    # answer.
    code, out = _git("for-each-ref", "--contains", "HEAD", "--format=%(refname)", "refs/heads")
    return code != 0 or not out.strip()


def scope_bases(scope_label: str) -> list[str]:
    """Every commit whose content this change inherited rather than wrote.

    The base, plus each ancestry :func:`imported_parents` names. A count-baseline
    ratchet reads this to answer "was this file's count already this high in
    something that predates the change?" -- the count half of the correction
    :func:`imported_parents` makes for added lines.

    The BASE belongs here for the same reason its imports do. Once a merge has
    legitimately raised a file's count on the base branch, and the baseline
    cannot be raised to match, every later change needs somewhere honest to
    inherit that number from; the base is a real commit that predates the change
    and cannot contain anything the change wrote. What is deliberately NOT here
    is the tip's literal parent list: on GitHub's merge ref one of those is the
    PR head, and taking its counts as a ceiling would accept any regression the
    PR introduced.

    Empty only for an undeterminable scope, where the caller already judges the
    whole tree.
    """
    endpoints = _scope_endpoints(scope_label)
    if endpoints is None:
        return []
    return [endpoints[0]] + imported_parents(scope_label)


def _batch_show(commit: str, paths: list[str]):
    """Yield ``(path, contents)`` for each path that exists in ``commit``.

    One ``git cat-file --batch`` process for the whole list. A per-path
    ``git show`` is the obvious spelling and is unusable here: a fork sync's
    merge touches thousands of files and the merge correction reads each of them
    once per ancestry, so the process spawns -- not the reads -- become the whole
    runtime.

    A path absent from the commit is simply skipped; the caller's question is
    "did this content already exist", and a file that did not exist held none.
    """
    if not paths:
        return
    request = "".join(f"{commit}:{path}\n" for path in paths).encode("utf-8", "surrogateescape")
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=ROOT,
        input=request,
        capture_output=True,
    )
    if proc.returncode != 0:
        return
    out = proc.stdout
    offset = 0
    for path in paths:
        newline = out.find(b"\n", offset)
        if newline < 0:
            return
        header = out[offset:newline].decode("utf-8", "replace").split()
        offset = newline + 1
        # ``<oid> <type> <size>`` for a hit; ``<name> missing`` (no trailing
        # payload) for a path the commit does not carry.
        if len(header) != 3 or not header[2].isdigit():
            continue
        size = int(header[2])
        blob = out[offset : offset + size]
        offset += size + 1  # the batch format ends each payload with a newline
        yield path, blob.decode("utf-8", "replace")


def _read_many(source: str | None, paths: list[str]):
    """``(path, contents)`` for each readable path in ``source``, or the tree if None.

    A missing path is skipped either way: the question these feed is "did this
    content already exist here", and a file that is not there held none.
    """
    if source is not None:
        yield from _batch_show(source, paths)
        return
    for path in paths:
        try:
            yield path, (ROOT / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue


def file_at(commit: str, path: str) -> str | None:
    """A path's contents in ``commit``, or None when it did not exist there.

    ``errors="replace"`` for the reason it is used everywhere else here: a file
    that is not valid UTF-8 must yield a scannable string rather than raise
    inside ``subprocess``.
    """
    proc = subprocess.run(
        ["git", "--no-pager", "show", f"{commit}:{path}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else None


def added_lines(scope_label: str) -> dict[str, set[int]] | None:
    """Repo-relative path -> line numbers this change ADDED, or None.

    Uses the diff endpoints named by ``changed_paths``' label, so the added set
    and the changed-file set always describe the same diff. An unknown label (or
    a failing git) degrades to None -- the added-line rule is then skipped
    rather than guessed, and a caller's count rules still apply.

    The answer is then narrowed by :func:`_narrow_to_unwritten`: a line whose
    text a commit this change inherited already carried in the same file was not
    written here. This is a narrowing, not an exemption -- a line no ancestry
    carried, which is exactly what a conflict resolution or a hand edit writes,
    survives it and is still reported.
    """
    endpoints = _scope_endpoints(scope_label)
    if endpoints is None:
        return None
    base, tip, content_tip = endpoints
    # A second endpoint only where the working tree is not the tip. Everywhere
    # else the diff runs to the tree, which is what the gates scan.
    args = ["diff", "--unified=0", base] + ([tip] if content_tip is not None else [])
    added = _added_from_diff(args)
    if added is None:
        return None
    return _narrow_to_unwritten(content_tip, scope_bases(scope_label), added)


def _narrow_to_unwritten(
    tip: str | None,
    bases: list[str],
    added: dict[str, set[int]],
) -> dict[str, set[int]]:
    """Drop added lines whose text a pre-existing ancestry already had in that file.

    Position is the wrong key for a merge. A two-parent diff reports a line as
    added whenever it MOVED -- an import that upstream left untouched but shifted
    by an insertion above it reads as brand new against the other parent, and on
    a fork sync that is most of the diff. So the question asked here is about
    content: does this exact line already exist somewhere in this file in an
    ancestry that predates the merge?

    Two consequences are deliberate:

    * **Whole-file, not same-position**, precisely so a move is not a write.
    * **An exactly duplicated line is treated as inherited.** Re-adding a line
      the file already contains elsewhere is the one thing this cannot tell
      apart from a move, and accepting it is the safe direction for a rule whose
      alternative is thousands of false reds with no in-diff remedy. What it
      still catches is the case the rule exists for: text no ancestry contained,
      which is what a conflict resolution and a hand edit both produce.
    """
    if not bases or not added:
        return added
    paths = sorted(added)
    # Only the added lines' own text is needed, so the tip's contents are reduced
    # to that immediately -- holding thousands of full files would not fit.
    tip_text: dict[str, dict[int, str]] = {}
    for path, contents in _read_many(tip, paths):
        lines = contents.splitlines()
        tip_text[path] = {
            number: lines[number - 1].strip() for number in added[path] if 1 <= number <= len(lines)
        }
    remaining = {path: set(numbers) for path, numbers in added.items()}
    for base in bases:
        for path, contents in _read_many(base, paths):
            candidates = remaining.get(path)
            if not candidates:
                continue
            inherited = {line.strip() for line in contents.splitlines()}
            texts = tip_text.get(path, {})
            remaining[path] = {
                number for number in candidates if texts.get(number, "\0") not in inherited
            }
    return {path: numbers for path, numbers in remaining.items() if numbers}


# ---------------------------------------------------------------------------
# Explicit-base entry points, for the env-base gate family
# ---------------------------------------------------------------------------


def parse_added_lines(diff_text: str, *, anchor_deletions: bool = False) -> set[int]:
    """1-based post-image line numbers the hunk headers in ``diff_text`` mark.

    For ONE file's ``--unified=0`` diff: only ``@@`` headers are read, so the
    caller must have scoped the diff to a single path
    (``git diff <frm> -- <path>``). There is deliberately no ``+++``
    attribution here — git QUOTES a path holding a non-ASCII byte on those
    lines, and a ``+++ b/`` parser silently drops that file's hunks. Path
    discovery belongs to :func:`changed_paths_at`, whose ``-z`` output is
    never quoted.

    A deletion-only hunk reads ``+<start>,0``: the change removed lines and
    added none, so by default it contributes nothing (``range(start, start)``
    is empty). ``anchor_deletions=True`` records ``start`` instead, for a gate
    that must see WHERE lines were removed — the focus-cue gate exists to
    catch a deleted cue line, which is invisible to the pure added set.
    """
    lines: set[int] = set()
    for raw in diff_text.splitlines():
        match = _HUNK_RE.match(raw)
        if match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count == 0:
            if anchor_deletions:
                lines.add(start)
            continue
        lines.update(range(start, start + count))
    return lines


def resolve_base(base: str) -> str:
    """The commit an env-provided base ref measures against.

    ``merge-base`` is the honest divergence point, but a shallow CI clone
    fetches the base commit as its own tip with no shared history, so it often
    has none — the base tip is then the fallback. This is resolution, not
    parsing: it is the one step the env-base family does differently from the
    resolver above, so it stays a separate function the gates call once per
    run.
    """
    code, out = _git("merge-base", base, "HEAD")
    return out.strip() if code == 0 else base


def changed_paths_at(frm: str) -> list[str]:
    """Paths the diff from ``frm`` to the WORKING TREE touches, in git's order.

    The explicit-base counterpart of :func:`changed_paths`: the caller names
    the base (CI passes the PR's ``base.sha`` through ``*_BASE_REF``, so a run
    never picks up base moves landing after it started) and applies its own
    scope filter to the returned paths. ``-z`` is what makes the answer
    trustworthy: without it git quotes any path holding an unusual byte, and a
    parser reading quoted output silently drops that file — a gate that skips
    a changed file is worse than no gate. ``--diff-filter=d`` drops deletions:
    a removed file has no lines to judge.

    A list, not a set, because git emits the names path-sorted and the
    consuming gates' reports inherit that order — a set would make a
    violation listing nondeterministic. A failing git raises
    ``subprocess.CalledProcessError`` so each caller can fail CLOSED with its
    own gate-named message; unlike :func:`changed_paths`, which degrades to
    whole-tree scope, an env-base gate that cannot see its base must refuse to
    pass, not widen.
    """
    out = _git_strict("diff", "--name-only", "-z", "--diff-filter=d", frm)
    return [p for p in out.split("\0") if p]


def added_lines_at(frm: str, path: str, *, anchor_deletions: bool = False) -> set[int]:
    """1-based line numbers the diff from ``frm`` to the working tree adds.

    Base-to-working-tree, so a local run sees edits that are not committed
    yet, which is the only form in which a local run is useful; CI checks out
    a clean tree where that equals base-to-HEAD. ``--text`` forces hunks even
    for a path ``.gitattributes`` marks ``-diff``: git would otherwise report
    only "Binary files differ", leaving nothing to scan and passing the file
    silently. Per PATH rather than whole-diff, matching how the env-base gates
    consume it — see :func:`parse_added_lines` for why single-path diffs need
    no ``+++`` attribution.
    """
    diff = _git_strict("diff", "--unified=0", "--no-color", "--text", frm, "--", path)
    return parse_added_lines(diff, anchor_deletions=anchor_deletions)
