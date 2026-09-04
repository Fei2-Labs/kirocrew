"""The shared scope resolver must tell the two merge shapes apart.

``scripts/ratchet_scope.py`` answers "which files did THIS change touch" for the
merge-ref ratchets. Two checkout shapes both look like "HEAD is a merge" and
need opposite diffs:

* CI's ``pull_request`` merge ref: the BASE is the first parent, so
  ``HEAD^1..HEAD`` is exactly the PR's own change.
* A local ``git merge origin/main`` on a feature branch: the FEATURE tip is the
  first parent, so ``HEAD^1..HEAD`` is only what main brought in and the
  branch's own commits are invisible -- every consuming gate then under-scopes,
  and a violation added in an earlier feature commit passes locally only to red
  the PR on CI.

The resolver decides by asking git which parent the base branch can reach, so
these tests build one synthetic repo per shape and pin the attempt LABEL chosen
plus the returned path set.

A merge also has TWO ancestries, and ``TestMergeInheritsTwoAncestries`` pins the
correction for that: content a parent already carried was not written by the
merge, so it is not judged as added. Those tests come in pairs on purpose,
because the correction and the hole it must not become are one code path -- for
every accepted-inheritance case there is a case that must still fail, including
the one that matters most, GitHub's merge ref, whose second parent is the PR
itself. The three-dot fallback deliberately keeps diffing
from ``merge-base(base, HEAD)`` rather than the base tip: an unscoped gate has
already been observed reporting files the base branch merged after the baseline
was taken, and the CI-shape test locks that property by moving main after the
branch point.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS, git_command_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ratchet_scope.py"

SPEC = importlib.util.spec_from_file_location("ratchet_scope", SCRIPT)
assert SPEC and SPEC.loader
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)


def _fixture_git_env() -> dict[str, str]:
    """Env for a fixture git call: no inherited location, templates, hooks, or identity.

    ``git_command_env()`` (the production chokepoint) strips the ``GIT_DIR``
    location family -- those must be ABSENT, and a merge over ``os.environ``
    can only add keys -- and pins the fixed-key exec vectors. On top of that,
    an inherited ``GIT_TEMPLATE_DIR`` (or a global ``init.templateDir``) would
    have its hooks COPIED into every fixture repo by ``git init`` and executed
    by the ``git commit`` below -- host-side effects from running the test
    suite -- so both template channels are pinned empty. Identity is supplied
    so a commit cannot depend on, or fall back to, the developer's global
    config, which is itself pointed at ``os.devnull``.
    """
    env = {
        **git_command_env(),
        "GIT_TEMPLATE_DIR": "",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    count = int(env["GIT_CONFIG_COUNT"])
    env[f"GIT_CONFIG_KEY_{count}"] = "init.templateDir"
    env[f"GIT_CONFIG_VALUE_{count}"] = ""
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=_fixture_git_env(),
    )
    return proc.stdout.strip()


def _commit_file(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)


def _repo_with_diverged_feature(tmp_path: Path) -> Path:
    """One base repo both shapes start from.

    ``main`` gains ``mainline.txt`` AFTER ``feature`` branches off with its own
    ``feature.py``, so the two sides of every merge below differ and a wrong
    parent choice shows up in the returned path set, not just the label.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main", ".")
    _commit_file(repo, "base.txt", "base")
    _git(repo, "checkout", "-b", "feature")
    _commit_file(repo, "feature.py", "the change under judgment")
    _git(repo, "checkout", "main")
    _commit_file(repo, "mainline.txt", "someone else's change, landed after the branch point")
    return repo


def _set_origin_main(repo: Path) -> None:
    # The synthetic repo has no remote; the resolver only needs the REF, so
    # point origin/main at the local main tip directly.
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The fixture's own git calls build a scrubbed env per call, but the
    # RESOLVER under test runs git with the ambient process environment: an
    # exported GIT_DIR (pytest run from a git hook, `git rebase --exec`,
    # `git bisect run`) would override its cwd=ROOT and answer for the wrong
    # repository. Delete the whole location family -- monkeypatch restores it
    # after the test -- using the same canonical list the production env
    # builder strips.
    for var in _GIT_LOCATION_VARS:
        monkeypatch.delenv(var, raising=False)
    fixture_repo = _repo_with_diverged_feature(tmp_path)
    # The module runs git with cwd=ROOT; retarget it at the synthetic repo.
    monkeypatch.setattr(scope, "ROOT", fixture_repo)
    return fixture_repo


class TestMergeShapes:
    def test_ci_merge_ref_scopes_to_the_change_only(self, repo: Path) -> None:
        # GitHub's pull_request merge ref: merge the PR INTO the base, so the
        # base tip is the first parent and origin/main can reach it.
        _set_origin_main(repo)
        _git(repo, "checkout", "--detach", "main")
        _git(repo, "merge", "--no-ff", "-m", "merge ref", "feature")

        paths, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        # Exactly the PR's own change: mainline.txt landed on the base after
        # the branch point and must NOT be judged as part of this change.
        assert paths == {"feature.py"}

    def test_local_merge_of_main_scopes_to_the_branch_own_commits(self, repo: Path) -> None:
        # The inverted shape: `git merge origin/main` ON the feature branch
        # puts the feature tip first. HEAD^1..HEAD here is what main brought
        # in, so taking the merge diff would hide feature.py -- the defect this
        # resolver exists to avoid. The base-reachability probe must reject the
        # merge attempts and fall through to the three-dot diff.
        _set_origin_main(repo)
        _git(repo, "checkout", "feature")
        _git(repo, "merge", "--no-ff", "-m", "sync with main", "origin/main")

        paths, label = scope.changed_paths()

        assert label == "origin/main...HEAD"
        assert paths == {"feature.py"}

    def test_merge_made_on_main_is_still_recognised_without_a_remote(self, repo: Path) -> None:
        # A merge made ON main itself (no remote at all): the prior main tip is
        # the first parent, and the local `main` ref -- now the merge commit --
        # reaches it. The probe must accept this via the `main` fallback;
        # probing only origin/main would reject it, and `main...HEAD` then
        # diffs the merge against itself: an EMPTY scope, so every consuming
        # gate passes vacuously -- a false green in the same direction as the
        # under-scope this resolver exists to prevent.
        _git(repo, "merge", "--no-ff", "-m", "land feature", "feature")

        paths, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        assert paths == {"feature.py"}

    def test_unverifiable_base_falls_through_rather_than_trusting_parent_order(
        self, repo: Path
    ) -> None:
        # No origin/main at all: the reachability probe cannot verify either
        # way, and an unverified merge diff is the failure mode above. Falling
        # through is the safe direction -- here the local `main` ref still
        # answers the three-dot question correctly.
        _git(repo, "checkout", "feature")
        _git(repo, "merge", "--no-ff", "-m", "sync with main", "main")

        paths, label = scope.changed_paths()

        assert label == "main...HEAD"
        assert paths == {"feature.py"}


class TestWholeTreeOverride:
    """A push to the base branch has no diff, and that is not a clean tree.

    The Main Ratchet Audit lane runs on a push to ``main``, where the checkout
    leaves HEAD, ``main`` and ``origin/main`` all at the pushed commit. The
    three-dot fallback then succeeds with an EMPTY path set -- exit 0, no
    attempt fails, nothing marks the answer as unusable -- and every consuming
    ratchet filters its violations against that empty set and reports green
    whatever the tree holds. The override is how a caller states that the
    question really is about a whole tree, instead of inheriting an answer that
    depends on which checkout shape a run happened to get.
    """

    def test_a_push_to_the_base_branch_resolves_to_an_empty_diff(self, repo: Path) -> None:
        # The hazard itself, pinned: HEAD *is* origin/main, so the scope is
        # empty rather than undeterminable, and a gate cannot tell the
        # difference between this and a change that touched nothing.
        _set_origin_main(repo)

        paths, label = scope.changed_paths()

        assert label == "origin/main...HEAD"
        assert paths == set()

    def test_the_override_answers_whole_tree(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_origin_main(repo)
        monkeypatch.setenv(scope.WHOLE_TREE_ENV, "1")

        paths, label = scope.changed_paths()

        assert paths is None
        assert scope.WHOLE_TREE_ENV in label

    def test_the_override_wins_over_a_resolvable_diff(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `feature` has a real diff against main, so without the override this
        # shape scopes to one file. The answer must not depend on that: a caller
        # asking for the whole tree gets it in every checkout shape.
        _git(repo, "checkout", "feature")
        _set_origin_main(repo)
        monkeypatch.setenv(scope.WHOLE_TREE_ENV, "1")

        paths, _ = scope.changed_paths()

        assert paths is None

    def test_a_blank_value_is_not_an_opt_in(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Actions writes an empty string for an unset expression, so a blank
        # value must read as absent rather than silently widening every gate.
        _set_origin_main(repo)
        monkeypatch.setenv(scope.WHOLE_TREE_ENV, "  ")

        paths, label = scope.changed_paths()

        assert paths == set()
        assert label == "origin/main...HEAD"

    def test_the_whole_tree_label_is_not_read_as_a_diff_range(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `added_lines` dispatches on the label, so an unrecognised one must
        # degrade to None (the added-line rule is skipped) rather than being
        # handed to `git diff` as a revision. The label comes from the resolver
        # itself: a hardcoded copy would keep passing after the production
        # label grew a `...`-shaped suffix that IS read as a revision.
        _set_origin_main(repo)
        monkeypatch.setenv(scope.WHOLE_TREE_ENV, "1")
        _, label = scope.changed_paths()

        assert scope.added_lines(label) is None

    def test_the_audit_lane_opts_in(self) -> None:
        # The wiring, not the mechanism: without this env the Main Ratchet Audit
        # runs four gates over zero files and reports a green verdict on main
        # that means nothing -- which is the exact false all-clear the lane
        # exists to prevent.
        workflow = ROOT / ".github" / "workflows" / "main-ratchet-audit.yml"
        body = workflow.read_text(encoding="utf-8")

        assert f"{scope.WHOLE_TREE_ENV}:" in body, (
            f"{workflow.name} no longer sets {scope.WHOLE_TREE_ENV}, so its black / "
            "subprocess-encoding / agent-SDK / sync-IO gates scope to the push's "
            "own diff -- which on main is empty, making all four pass by measuring "
            "nothing"
        )


class TestExplicitBase:
    """The env-base family's entry points: an EXPLICIT ref in, shared parsing out.

    ``check_brand_name.py``, ``check_harness_parity.py`` and
    ``check_focus_cue.py`` are handed their base through ``*_BASE_REF`` (CI
    resolves it to the PR's ``base.sha``), so unlike the resolver above they
    never discover the checkout shape — but the diff parsing must be the same
    code, or the same added line gets judged differently by different gates.
    """

    def test_the_entry_points_are_reachable_by_name(self) -> None:
        # The gates call these through a path-loaded module, so a rename there
        # must fail HERE, not as an AttributeError inside a CI run.
        assert callable(scope.resolve_base)
        assert callable(scope.changed_paths_at)
        assert callable(scope.added_lines_at)
        assert callable(scope.parse_added_lines)

    def test_changed_paths_at_diffs_from_the_named_commit_only(self, repo: Path) -> None:
        # The base.sha property: an explicit commit in, exactly the work after
        # it out — moving any branch ref afterwards must change nothing,
        # because a run started against base.sha must not pick up base moves
        # landing after it started.
        base = _git(repo, "rev-parse", "main~1")
        _git(repo, "branch", "-f", "release", "main")  # a ref move, post-capture

        assert scope.changed_paths_at(base) == ["mainline.txt"]

    def test_changed_paths_at_sees_the_working_tree(self, repo: Path) -> None:
        # Base-to-working-tree: a local run must see edits that are not
        # committed yet, which is the only form in which a local run is useful.
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "base.txt").write_text("edited, not committed\n", encoding="utf-8")

        assert scope.changed_paths_at(base) == ["base.txt"]

    def test_changed_paths_at_does_not_quote_a_non_ascii_path(self, repo: Path) -> None:
        # `-z` output is never quoted, so a path with non-ASCII bytes comes
        # back byte-exact — usable as a pathspec for the per-path diff. A
        # quoted `"b/\346..."` name is how a parser silently drops a file.
        base = _git(repo, "rev-parse", "HEAD")
        _commit_file(repo, "日本語.md", "non-ascii name")

        assert scope.changed_paths_at(base) == ["日本語.md"]
        assert scope.added_lines_at(base, "日本語.md") == {1}

    def test_changed_paths_at_fails_closed_on_an_unresolvable_base(self, repo: Path) -> None:
        # The env-base gates refuse to pass when they cannot see their base —
        # unlike changed_paths(), which degrades to whole-tree scope. The
        # raised error is the seam each gate wraps in its own fail-closed
        # message.
        with pytest.raises(subprocess.CalledProcessError):
            scope.changed_paths_at("no-such-ref")

    def test_added_lines_at_names_the_added_lines(self, repo: Path) -> None:
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "base.txt").write_text("base.txt\nnew two\nnew three\n", encoding="utf-8")

        assert scope.added_lines_at(base, "base.txt") == {2, 3}

    def test_a_pure_deletion_contributes_nothing_by_default(self, repo: Path) -> None:
        (repo / "three.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        _git(repo, "add", "three.txt")
        _git(repo, "commit", "-m", "three lines")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "three.txt").write_text("one\nthree\n", encoding="utf-8")

        assert scope.added_lines_at(base, "three.txt") == set()

    def test_anchor_deletions_marks_where_the_lines_were_removed(self, repo: Path) -> None:
        # The focus-cue gate's semantics: the edit that most often removes a
        # cue is a pure deletion, invisible to the added set, so the `+N,0`
        # hunk is anchored at N instead of dropped.
        (repo / "three.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        _git(repo, "add", "three.txt")
        _git(repo, "commit", "-m", "three lines")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "three.txt").write_text("one\nthree\n", encoding="utf-8")

        assert scope.added_lines_at(base, "three.txt", anchor_deletions=True) == {1}

    def test_resolve_base_prefers_the_merge_base(self, repo: Path) -> None:
        # main moved after feature branched off; measuring from the main TIP
        # would charge main's own commits to the feature diff. The honest
        # divergence point is the merge-base.
        _git(repo, "checkout", "feature")

        expected = _git(repo, "merge-base", "main", "HEAD")
        assert scope.resolve_base("main") == expected
        assert scope.resolve_base("main") != _git(repo, "rev-parse", "main")

    def test_resolve_base_falls_back_to_the_base_tip(self, repo: Path) -> None:
        # A shallow CI clone fetches the base as its own tip with no shared
        # history: merge-base fails, and the ref itself has to serve.
        _git(repo, "checkout", "--orphan", "detached")
        _git(repo, "commit", "-m", "unrelated root")

        assert scope.resolve_base("main") == "main"


class TestParseAddedLines:
    """The text-level parser, reachable without a repository."""

    DIFF = (
        "diff --git a/a.txt b/a.txt\n"
        "index 000..111 100644\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1,0 +2,3 @@ some context\n"
        "+two\n"
        "+three\n"
        "+four\n"
        "@@ -9,2 +11,0 @@\n"
        "-gone\n"
        "-gone\n"
        "@@ -20 +21 @@\n"
        "-old\n"
        "+new\n"
    )

    def test_added_hunks_with_and_without_counts(self) -> None:
        # `+2,3` names three lines; a bare `+21` means exactly one; the
        # deletion-only `+11,0` contributes nothing by default.
        assert scope.parse_added_lines(self.DIFF) == {2, 3, 4, 21}

    def test_anchor_deletions_adds_the_deletion_point(self) -> None:
        assert scope.parse_added_lines(self.DIFF, anchor_deletions=True) == {2, 3, 4, 11, 21}


def test_env_base_gates_delegate_to_the_shared_plumbing() -> None:
    """Every added-line gate reads its diff through ratchet_scope.

    A private hunk parser per gate is the divergence this module exists to
    close: the same added line judged differently by different gates, and a
    scope fix to one copy leaving the others wrong. The hunk-header regex is
    the private parser's signature, so its absence is the pin — the focus-cue
    self-test may still grep raw ``@@`` lines to validate its PROBE's input,
    which is not parsing.
    """
    for name in ("check_brand_name.py", "check_harness_parity.py", "check_focus_cue.py"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "ratchet_scope.py" in source, f"{name} no longer uses the shared plumbing"
        assert r"\+(\d+)(?:,(\d+))?" not in source, f"{name} grew a private hunk parser back"


class TestMergeInheritsTwoAncestries:
    """A merge is judged against BOTH parents, and a truly-new line still fails.

    The correction and the hole it must not become are the same code path, so
    every test here comes in a pair: an inherited line is accepted, and a line
    no ancestry carried, in the same file of the same merge, is still reported.
    """

    @staticmethod
    def _import_merge(repo: Path) -> None:
        """A fork-sync shape: a real merge on main, both sides having edited one file.

        ``consumer.py`` starts with one line. ``main`` inserts a line ABOVE it,
        which is what makes the untouched line MOVE, and the imported branch
        adds a line of its own below.
        """
        (repo / "consumer.py").write_text("original = 1\n", encoding="utf-8")
        _git(repo, "add", "consumer.py")
        _git(repo, "commit", "-m", "seed the consumer")
        _git(repo, "checkout", "-b", "imported")
        (repo / "consumer.py").write_text("original = 1\nfrom_upstream = 2\n", encoding="utf-8")
        _git(repo, "commit", "-am", "upstream adds a line")
        _git(repo, "checkout", "main")
        (repo / "consumer.py").write_text("inserted_above = 0\noriginal = 1\n", encoding="utf-8")
        _git(repo, "commit", "-am", "the fork inserts above")
        _git(repo, "merge", "--no-ff", "-m", "sync", "imported", "--no-edit")

    def test_a_line_only_moved_by_the_merge_is_not_reported_as_added(self, repo: Path) -> None:
        self._import_merge(repo)
        # The union of both sides: every line came from one or the other, so
        # nothing was written by the merge. git resolves this one itself, and
        # the merge commit is left as HEAD -- committing past it would make HEAD
        # an ordinary commit and change the scope shape under test.

        _, label = scope.changed_paths()
        assert label == "merge HEAD^1..HEAD"
        assert scope.added_lines(label).get("consumer.py", set()) == set()

    def test_a_line_no_ancestry_carried_is_still_reported(self, repo: Path) -> None:
        self._import_merge(repo)
        # Left UNCOMMITTED on purpose: the merge scope diffs to the working
        # tree, so this is what a resolver's own edit looks like to the gate
        # while they are still fixing it.
        (repo / "consumer.py").write_text(
            "inserted_above = 0\noriginal = 1\nfrom_upstream = 2\nwritten_by_hand = 3\n",
            encoding="utf-8",
        )

        _, label = scope.changed_paths()
        # Only the hand-written line: line 4. Accepting it too would make the
        # merge case a blanket exemption, which is the failure this pairs with.
        assert scope.added_lines(label).get("consumer.py") == {4}

    def test_the_imported_ancestry_is_named_and_the_own_history_is_not(self, repo: Path) -> None:
        self._import_merge(repo)
        imported = _git(repo, "rev-parse", "HEAD^2")
        base = _git(repo, "rev-parse", "HEAD^1")

        _, label = scope.changed_paths()
        parents = scope.imported_parents(label)

        assert parents == [imported]
        # The base is not "imported" -- it is where the change started. It is
        # a ceiling for the count rule, hence its presence in scope_bases.
        assert base not in parents
        assert scope.scope_bases(label) == [base, imported]

    def test_a_range_with_no_merge_in_it_imports_no_ancestry(self, repo: Path) -> None:
        # The fast-forward shape every ordinary PR takes. Nothing was imported,
        # so `imported_parents` is empty -- but the BASE is still a ceiling,
        # because it is a real commit predating the change that cannot contain
        # anything the change wrote.
        _set_origin_main(repo)
        _git(repo, "checkout", "feature")
        base = _git(repo, "merge-base", "origin/main", "HEAD")

        _, label = scope.changed_paths()

        assert label == "origin/main...HEAD"
        assert scope.imported_parents(label) == []
        assert scope.scope_bases(label) == [base]

    def test_a_non_merge_scope_still_reports_a_plain_added_line(self, repo: Path) -> None:
        # The counterpart: the added-line answer for a non-merge scope is the
        # raw diff, with no content narrowing applied to it at all.
        _set_origin_main(repo)
        _git(repo, "checkout", "feature")
        (repo / "feature.py").write_text("feature.py\nnovel = 1\n", encoding="utf-8")
        _git(repo, "commit", "-am", "add a line")

        _, label = scope.changed_paths()

        # feature.py is wholly new against merge-base(main, HEAD), so BOTH of
        # its lines are added -- the raw diff answer, unnarrowed.
        assert scope.added_lines(label).get("feature.py") == {1, 2}

    def test_a_ci_merge_ref_does_not_treat_the_pr_head_as_pre_existing(self, repo: Path) -> None:
        # The dangerous direction. On GitHub's pull_request merge ref the second
        # parent is the PR itself, so taking both parents as pre-existing
        # history would accept every line the PR adds -- the gate deleted. The
        # PR head must NOT appear as an imported ancestry.
        _set_origin_main(repo)
        _git(repo, "checkout", "--detach", "main")
        _git(repo, "merge", "--no-ff", "-m", "merge ref", "feature", "--no-edit")
        pr_head = _git(repo, "rev-parse", "HEAD^2")

        _, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        assert pr_head not in scope.imported_parents(label)
        assert pr_head not in scope.scope_bases(label)
        # And the PR's own file is still judged as added.
        assert scope.added_lines(label).get("feature.py") == {1}

    def test_a_merge_nested_inside_a_ci_merge_ref_is_still_imported(self, repo: Path) -> None:
        # The shape a fork-sync PR actually presents to CI: the PR head is
        # itself a merge that imported foreign history. The walk must descend
        # THROUGH the PR head -- own history -- and still name the ancestry it
        # brought in, or the gate charges the PR with every upstream line.
        _git(repo, "checkout", "-b", "imported-upstream", "main")
        _commit_file(repo, "upstream.py", "foreign history")
        upstream = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "feature")
        _git(repo, "merge", "--no-ff", "-m", "sync upstream", upstream, "--no-edit")
        _git(repo, "checkout", "main")
        _set_origin_main(repo)
        _git(repo, "checkout", "--detach", "main")
        _git(repo, "merge", "--no-ff", "-m", "merge ref", "feature", "--no-edit")

        _, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        assert scope.imported_parents(label) == [upstream]

    def test_the_pull_request_env_signal_alone_forces_the_strict_direction(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A real merge ON a branch, so the detachment signal says "not a merge
        # ref" -- but GITHUB_BASE_REF is set, which only a pull_request event
        # does. Either signal is enough, and the strict reading must win: the
        # second parent is then the change, never pre-existing history.
        monkeypatch.setenv("GITHUB_BASE_REF", "main")
        self._import_merge(repo)

        _, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        assert scope.imported_parents(label) == []

    @staticmethod
    def _import_merge_on_a_side_branch(repo: Path) -> str:
        """The same fork-sync shape, but on a branch ``main`` never reaches.

        The on-main variant above cannot express a merge buried in the RANGE:
        committing on main afterwards moves the base past the merge, so the base
        contains it and every commit of interest is between it and the tip.

        ``consumer.py`` is seeded ON MAIN, so it predates the base: a line only
        MOVED by the merge has to be a line the base already had, and a file
        created inside the range is wholly new to it. Returns the imported
        branch's tip; ``main`` is left where the seed put it, so a caller sets
        ``origin/main`` AFTER calling this.
        """
        (repo / "consumer.py").write_text("original = 1\n", encoding="utf-8")
        _git(repo, "add", "consumer.py")
        _git(repo, "commit", "-m", "seed the consumer on the base")
        _git(repo, "checkout", "-b", "work", "main")
        _git(repo, "checkout", "-b", "imported-upstream")
        (repo / "consumer.py").write_text("original = 1\nfrom_upstream = 2\n", encoding="utf-8")
        _git(repo, "commit", "-am", "upstream adds a line")
        imported = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "work")
        (repo / "consumer.py").write_text("inserted_above = 0\noriginal = 1\n", encoding="utf-8")
        _git(repo, "commit", "-am", "the fork inserts above")
        _git(repo, "merge", "--no-ff", "-m", "sync", "imported-upstream", "--no-edit")
        return imported

    def test_a_non_merge_tip_still_inherits_a_merge_inside_its_range(self, repo: Path) -> None:
        # The defect that survived keying on "HEAD is a merge": one ordinary
        # commit on top of a fork-sync merge makes HEAD single-parent, and the
        # whole imported diff was attributed to the change again. The merge is
        # still in the range, so the ancestry it imported is still inherited.
        imported = self._import_merge_on_a_side_branch(repo)
        _set_origin_main(repo)
        _commit_file(repo, "afterwards.txt", "an ordinary commit on top of the merge")
        assert len(_git(repo, "log", "-1", "--format=%p").split()) == 1, "tip must be non-merge"

        _, label = scope.changed_paths()

        assert label == "origin/main...HEAD"
        assert scope.imported_parents(label) == [imported]

    def test_the_narrowing_still_applies_through_a_non_merge_tip(self, repo: Path) -> None:
        # The same generalization, end to end, with the merge buried behind an
        # ordinary commit instead of sitting at the tip. `consumer.py` ends as
        #   1 inserted_above = 0   <- written on this branch, inside the range
        #   2 original = 1         <- the base's line, only MOVED down by (1)
        #   3 from_upstream = 2    <- the imported ancestry's line
        # so exactly one of the three is this change's to answer for. Line 2 is
        # the one the correction exists for: nothing but its position changed.
        self._import_merge_on_a_side_branch(repo)
        _set_origin_main(repo)
        _commit_file(repo, "afterwards.txt", "an ordinary commit on top of the merge")

        _, label = scope.changed_paths()

        assert scope.added_lines(label).get("consumer.py") == {1}

        # And a line no ancestry carried is still reported when it lands beside
        # them, which is what stops this being an exemption.
        (repo / "consumer.py").write_text(
            "inserted_above = 0\noriginal = 1\nfrom_upstream = 2\nwritten_by_hand = 3\n",
            encoding="utf-8",
        )
        assert scope.added_lines(label).get("consumer.py") == {1, 4}

    def test_a_merge_deep_in_the_range_is_found_from_a_ci_merge_ref_too(self, repo: Path) -> None:
        # Both generalizations at once: a fork-sync merge, an ordinary commit on
        # top of it, and THEN GitHub's merge ref over that. The walk must
        # descend through the PR head, past the ordinary commit, and still name
        # the imported ancestry -- while never treating the PR head itself as
        # pre-existing.
        imported = self._import_merge_on_a_side_branch(repo)
        _commit_file(repo, "afterwards.txt", "the PR's own commit")
        pr_head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "main")
        _commit_file(repo, "base-moved.txt", "the base moves after the branch point")
        _set_origin_main(repo)
        _git(repo, "checkout", "--detach", "main")
        _git(repo, "merge", "--no-ff", "-m", "merge ref", "work", "--no-edit")

        _, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        assert scope.imported_parents(label) == [imported]
        assert pr_head not in scope.scope_bases(label)
