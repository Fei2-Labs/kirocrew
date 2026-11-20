"""Tests for project git status discovery."""

from __future__ import annotations

import os
import shutil
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_project_git_status_log import (  # noqa: F401
    _git,
    _make_app,
    _rmtree,
    discovery_stops_at_tmp_path_fixture,
    mock_sel_fixture,
    outside_any_repo_fixture,
    passthrough_sandbox_fixture,
    repo_fixture,
    repo_template_fixture,
)

pytestmark = pytest.mark.usefixtures("passthrough_sandbox")


class TestMultiRepoDiscovery:
    """A project dir that is not itself a repo but contains them.

    A workspace laid out as one repo per package (``<ws>/src/<Package>``) has no
    repo at its own root, so upward-only resolution reported none at all.
    """

    @pytest.fixture()
    def workspace(self, tmp_path, _repo_template):
        """`ws/src/{PkgA,PkgB}` repos under a plain, non-repo workspace root."""
        src = tmp_path / "ws" / "src"
        src.mkdir(parents=True)
        for pkg in ("PkgA", "PkgB"):
            shutil.copytree(_repo_template, src / pkg)
        return tmp_path / "ws"

    @pytest.mark.asyncio
    async def test_groups_changes_per_descendant_repo(self, workspace, mock_sel):
        (workspace / "src" / "PkgA" / "a.txt").write_text("changed in A\n")
        (workspace / "src" / "PkgB" / "fresh.txt").write_text("new in B\n")

        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            resp = await client.get(f"/api/project/git/status?path={workspace}")
            data = await resp.json()

        assert data["repo"] is True
        groups = {g["name"]: g for g in data["repos"]}
        assert set(groups) == {os.path.join("src", "PkgA"), os.path.join("src", "PkgB")}
        assert [f["path"] for f in groups[os.path.join("src", "PkgA")]["files"]] == ["a.txt"]
        assert [f["path"] for f in groups[os.path.join("src", "PkgB")]["files"]] == ["fresh.txt"]
        assert all(g["branch"] == "trunk" for g in data["repos"])
        assert "branch" not in data
        # Nothing renders per-group ahead/behind, so the response does not carry it.
        assert all("ahead" not in g and "behind" not in g for g in data["repos"])

    @pytest.mark.asyncio
    async def test_every_row_names_its_own_repo(self, workspace, mock_sel):
        """`files` stays flat for existing consumers, with a per-row repoRoot."""
        (workspace / "src" / "PkgA" / "a.txt").write_text("changed in A\n")
        (workspace / "src" / "PkgB" / "a.txt").write_text("changed in B\n")

        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            resp = await client.get(f"/api/project/git/status?path={workspace}")
            data = await resp.json()

        assert [f["path"] for f in data["files"]] == ["a.txt", "a.txt"]
        roots = sorted(f["repoRoot"] for f in data["files"])
        assert roots == sorted(g["root"] for g in data["repos"])
        assert sum(len(g["files"]) for g in data["repos"]) == len(data["files"])

    @pytest.mark.asyncio
    async def test_a_group_starved_by_the_row_budget_says_so(
        self, workspace, mock_sel, monkeypatch
    ):
        """A repo whose rows the shared budget dropped must not read as clean.

        Rendering its count as 0 asserts the repo has nothing changed -- about the
        one repo the budget stopped us from reading.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        for pkg in ("PkgA", "PkgB"):
            for n in range(3):
                (workspace / "src" / pkg / f"f{n}.txt").write_text("x\n")
        monkeypatch.setattr(files_mod, "_REPO_ROW_BUDGET", 2)
        files_mod._repo_scan_cache.clear()

        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            data = await (await client.get(f"/api/project/git/status?path={workspace}")).json()

        assert data["truncated"] is True
        assert len(data["files"]) == 2
        # BOTH cut shapes must be marked: the group whose rows the shared budget
        # dropped, AND the one that hit the per-repo cap inside `_run` while the
        # budget was still full -- the latter arrives already truncated, so a
        # length comparison alone cannot see it.
        assert all(g.get("truncated") for g in data["repos"]), [
            dict(name=g["name"], files=len(g["files"]), truncated=g.get("truncated"))
            for g in data["repos"]
        ]
        # No group may report an empty file list without saying it was cut short.
        for group in data["repos"]:
            if not group["files"]:
                assert group.get("truncated") is True, group["name"]

    @pytest.mark.asyncio
    async def test_clean_workspace_reports_no_files(self, workspace, mock_sel):
        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            resp = await client.get(f"/api/project/git/status?path={workspace}")
            data = await resp.json()
        assert data["repo"] is True
        assert data["files"] == []
        assert len(data["repos"]) == 2

    @pytest.mark.asyncio
    async def test_dir_with_no_repos_anywhere_still_reports_false(self, tmp_path, mock_sel):
        bare = tmp_path / "bare"
        (bare / "src" / "notarepo").mkdir(parents=True)
        async with TestClient(TestServer(_make_app(str(bare)))) as client:
            resp = await client.get(f"/api/project/git/status?path={bare}")
            data = await resp.json()
        assert data["repo"] is False
        assert data["files"] == []
        assert "repos" not in data

    @pytest.mark.asyncio
    async def test_single_descendant_repo_takes_the_grouped_shape(
        self, tmp_path, _repo_template, mock_sel
    ):
        """One descendant is the same case as several, not an ancestor lookup.

        Re-probing the non-repo base answers "not a repo" and empties the panel,
        and a bare single-repo answer would hide both the repo's name and a
        per-repo refusal.
        """
        ws = tmp_path / "solo"
        src = ws / "src"
        src.mkdir(parents=True)
        shutil.copytree(_repo_template, src / "OnlyPkg")
        (src / "OnlyPkg" / "a.txt").write_text("changed\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            resp = await client.get(f"/api/project/git/status?path={ws}")
            data = await resp.json()

        assert data["repo"] is True
        assert [f["path"] for f in data["files"]] == ["a.txt"]
        assert data["files"][0]["repoRoot"] == str(src / "OnlyPkg")
        assert [g["name"] for g in data["repos"]] == [os.path.join("src", "OnlyPkg")]
        assert data["repos"][0]["files"] == data["files"]

    @pytest.mark.asyncio
    async def test_lone_descendant_refusal_is_visible_as_a_group(
        self, tmp_path, _repo_template, mock_sel
    ):
        """A repo refused for a filter driver must not read as clean."""
        ws = tmp_path / "filtered"
        src = ws / "src"
        src.mkdir(parents=True)
        pkg = src / "OnlyPkg"
        shutil.copytree(_repo_template, pkg)
        _git(pkg, "config", "filter.evil.clean", "touch /tmp/pwned")
        (pkg / "a.txt").write_text("changed\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            resp = await client.get(f"/api/project/git/status?path={ws}")
            data = await resp.json()

        assert data["repos"][0]["refused"] is True
        assert data["repos"][0]["files"] == []
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_a_collection_that_raises_is_a_refused_group(
        self, workspace, mock_sel, monkeypatch
    ):
        """An error collecting one repo marks THAT group refused; the sibling
        still reports, and the failing repo does not vanish from the list."""
        from kiro_crew.dashboard.handlers import files as files_mod

        broken = str(workspace / "src" / "PkgB")
        real_run = files_mod._run_git_bounded

        def _fail_in_pkg_b(args, cwd, *a, **kw):
            if os.path.realpath(cwd) == os.path.realpath(broken):
                raise OSError("injected git spawn failure")
            return real_run(args, cwd, *a, **kw)

        monkeypatch.setattr(files_mod, "_run_git_bounded", _fail_in_pkg_b)
        files_mod._repo_scan_cache.clear()
        (workspace / "src" / "PkgA" / "a.txt").write_text("changed in A\n")

        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            resp = await client.get(f"/api/project/git/status?path={workspace}")
            data = await resp.json()

        assert resp.status == 200
        groups = {g["name"]: g for g in data["repos"]}
        assert groups[os.path.join("src", "PkgB")]["refused"] is True
        assert groups[os.path.join("src", "PkgB")]["files"] == []
        assert [f["path"] for f in groups[os.path.join("src", "PkgA")]["files"]] == ["a.txt"]

    @pytest.mark.asyncio
    async def test_single_repo_refusal_is_reported_on_the_response(self, repo, mock_sel):
        """The project dir being the repo draws no group header, so the refusal
        has to reach the client on its own.

        The refusal is a 503 carrying ``git_status_filter_refused``, not a 200
        with an empty file list: an empty list is indistinguishable from a clean
        repo, so the panel would draw its green pill over a tree it holds no
        status for. A single-repo refusal must reach the client rather than read
        as clean.
        """
        _git(repo, "config", "filter.evil.clean", "touch /tmp/pwned")
        (repo / "a.txt").write_text("changed\n")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data["code"] == "git_status_filter_refused"
        assert data["cause"] == "declared"

    @pytest.mark.asyncio
    async def test_capped_discovery_says_the_list_is_partial(
        self, tmp_path, _repo_template, mock_sel, monkeypatch
    ):
        """A repo dropped by a discovery bound must not read as nonexistent.

        Same failure mode the row-budget notice exists to prevent: a short list
        rendered as the whole workspace.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        ws = tmp_path / "big"
        src = ws / "src"
        src.mkdir(parents=True)
        for name in ("PkgA", "PkgB", "PkgC"):
            shutil.copytree(_repo_template, src / name)
        monkeypatch.setattr(files_mod, "_REPO_SCAN_MAX_REPOS", 2)
        files_mod._repo_scan_cache.clear()

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            resp = await client.get(f"/api/project/git/status?path={ws}")
            data = await resp.json()

        assert len(data["repos"]) == 2
        assert data["reposTruncated"] is True

    @pytest.mark.asyncio
    async def test_cached_root_swapped_for_an_outside_symlink_is_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """A cached root is re-checked at use time, not trusted for the TTL.

        Discovery refuses a symlinked child, but its answer is cached -- so a child
        replaced by a symlink out of the project inside that window would run git
        in an unauthorised directory and return its branch and paths.
        """
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        pkg = src / "PkgA"
        shutil.copytree(_repo_template, pkg)
        (pkg / "a.txt").write_text("inside the project\n")

        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "secret-branch")
        (outside / "confidential.txt").write_text("must not leak\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            warm = await (await client.get(f"/api/project/git/status?path={ws}")).json()
            assert [f["path"] for f in warm["files"]] == ["a.txt"]

            # Same path, now pointing out of the project, while the scan is cached.
            _rmtree(pkg)
            os.symlink(outside, pkg)

            after = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        assert after.get("repos", []) == []
        assert after["files"] == []
        assert "confidential.txt" not in str(after)
        assert "secret-branch" not in str(after)

    @pytest.mark.skipif(os.name == "nt", reason="os.symlink needs elevation on Windows")
    @pytest.mark.asyncio
    async def test_a_root_swapped_after_verification_is_refused(
        self, tmp_path, _repo_template, mock_sel, monkeypatch
    ):
        """Verification and the git spawn must agree on ONE identity.

        Re-resolving the root at use time proves only where the path points NOW, so
        a swap inside the window between the two passes every check.
        """
        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "secret-branch")
        (outside / "confidential.txt").write_text("must not leak\n")

        src = tmp_path / "ws" / "src"
        src.mkdir(parents=True)
        pkg = src / "PkgA"
        shutil.copytree(_repo_template, pkg)

        from kiro_crew.dashboard.handlers import files as files_mod

        real_verify = files_mod._verify_descendant_roots

        def _verify_then_swap(base: str, roots: list[str]):
            kept = real_verify(base, roots)
            # The window: verification has passed, git has not run yet.
            _rmtree(pkg)
            os.symlink(outside, pkg)
            return kept

        monkeypatch.setattr(files_mod, "_verify_descendant_roots", _verify_then_swap)

        async with TestClient(TestServer(_make_app(str(tmp_path / "ws")))) as client:
            data = await (
                await client.get(f"/api/project/git/status?path={tmp_path / 'ws'}")
            ).json()

        blob = str(data)
        assert "confidential.txt" not in blob, blob
        assert "secret-branch" not in blob, blob

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-shaped core.worktree path")
    @pytest.mark.asyncio
    async def test_a_contained_repo_pointing_its_worktree_outside_is_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """`.git` under the project is not enough: `core.worktree` moves the tree.

        The directory gate only ever sees a contained `.git`, and the grouped path
        skips the toplevel probe because discovery already named the root -- so
        without an effective-worktree check git happily reports a tree the project
        does not contain.
        """
        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "secret-branch")
        (outside / "confidential.txt").write_text("must not leak\n")

        src = tmp_path / "ws" / "src"
        src.mkdir(parents=True)
        pkg = src / "PkgA"
        shutil.copytree(_repo_template, pkg)
        _git(pkg, "config", "core.worktree", str(outside))

        async with TestClient(TestServer(_make_app(str(tmp_path / "ws")))) as client:
            data = await (
                await client.get(f"/api/project/git/status?path={tmp_path / 'ws'}")
            ).json()

        blob = str(data)
        assert "confidential.txt" not in blob, blob
        assert "secret-branch" not in blob, blob
        # Refused, not silently empty: the panel has to be able to say why.
        groups = data.get("repos") or []
        assert not groups or all(g.get("refused") or not g["files"] for g in groups), blob

    @pytest.mark.asyncio
    async def test_a_symlinked_repo_inside_the_project_is_still_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """Containment is not the only test: a symlink is refused as such.

        Following one would report a repo twice under two names, and its target is
        only checkable at the moment of the check.
        """
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        real = src / "Real"
        shutil.copytree(_repo_template, real)
        (real / "a.txt").write_text("changed\n")
        os.symlink(real, src / "Alias")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            data = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        assert [g["name"] for g in data["repos"]] == [os.path.join("src", "Real")]

    @pytest.mark.asyncio
    async def test_a_git_dir_reached_through_a_link_is_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """The FORM of the indirection is never the test, only where it lands.

        A `.git` that is itself a link to an outside git directory reads as an
        ordinary directory to `isdir`, and a Windows junction is not even reported
        by `islink` -- so containment is decided on the resolved path instead.
        """
        ws = tmp_path / "ws"
        (ws / "src").mkdir(parents=True)
        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "linked-secret")

        planted = ws / "src" / "Planted"
        planted.mkdir()
        os.symlink(outside / ".git", planted / ".git")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            data = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        assert data.get("repos", []) == []
        assert "linked-secret" not in str(data)

    @pytest.mark.asyncio
    async def test_gitdir_pointer_out_of_the_project_is_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """A `.git` FILE can name a git directory anywhere.

        The candidate directory itself passes containment, so without following the
        pointer git runs against the outside repository and returns its branch plus
        its whole tracked file list -- absent locally, so reported as deletions.
        """
        ws = tmp_path / "ws"
        (ws / "src").mkdir(parents=True)
        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "secret-branch")
        (outside / "confidential.txt").write_text("must not leak\n")
        _git(outside, "add", "-A")
        _git(outside, "commit", "-qm", "secret")

        planted = ws / "src" / "Planted"
        planted.mkdir()
        (planted / ".git").write_text(f"gitdir: {outside / '.git'}\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            data = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        assert data.get("repos", []) == []
        assert data["files"] == []
        assert "secret-branch" not in str(data)
        assert "confidential.txt" not in str(data)

    @pytest.mark.asyncio
    async def test_a_linked_worktree_inside_the_project_still_works(
        self, tmp_path, _repo_template, mock_sel
    ):
        """The pointer form is legitimate when it stays inside the project."""
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        main_repo = src / "Main"
        shutil.copytree(_repo_template, main_repo)
        linked = src / "Linked"
        _git(main_repo, "worktree", "add", "-q", str(linked), "-b", "side")
        (linked / "a.txt").write_text("changed in the linked worktree\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            data = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        names = [g["name"] for g in data.get("repos", [])]
        assert os.path.join("src", "Linked") in names, names

    @pytest.mark.asyncio
    async def test_refresh_bypasses_the_root_scan_cache(self, workspace, _repo_template, mock_sel):
        """A repo cloned after the first poll appears on an explicit refresh."""
        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            first = await (await client.get(f"/api/project/git/status?path={workspace}")).json()
            shutil.copytree(_repo_template, workspace / "src" / "PkgC")
            cached = await (await client.get(f"/api/project/git/status?path={workspace}")).json()
            refreshed = await (
                await client.get(f"/api/project/git/status?path={workspace}&refresh=1")
            ).json()

        assert len(first["repos"]) == 2
        assert len(cached["repos"]) == 2
        assert len(refreshed["repos"]) == 3

    def test_a_huge_directory_is_never_materialised_whole(self, tmp_path, monkeypatch):
        """The read itself is bounded, not the loop that follows it.

        ``sorted(os.scandir(...))`` materialises a whole directory before any cap is
        consulted, so a tree with millions of entries exhausts memory however low the
        per-entry budget is. Asserted at the seam the code actually uses -- patching
        the global ``os`` instead would outlive the test on platforms that finalise
        ``tmp_path`` before ``monkeypatch``.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        for n in range(8):
            (tmp_path / f"d{n}").mkdir()
        requested: list[int] = []
        real_islice = files_mod.islice

        def counting_islice(iterable, n):
            requested.append(n)
            return real_islice(iterable, n)

        monkeypatch.setattr(files_mod, "_REPO_SCAN_MAX_ENTRIES", 4)
        monkeypatch.setattr(files_mod, "islice", counting_islice)
        # The probe's NOT-A-REPOSITORY verdict, spelled as git spells it. A bare
        # rc=1 with no stderr is an operational failure now, and discovery
        # refuses to scan downward from one (an outage must not be answered as
        # an empty workspace), so the stand-in has to carry the real verdict for
        # the scan under test to be reached at all.
        with patch.object(
            files_mod,
            "_run_git_bounded",
            return_value=(128, "fatal: not a git repository", False),
        ):
            files_mod._repo_scan_cache.clear()
            roots, bounded, _failed = files_mod._discover_repo_roots(str(tmp_path), ["git"], {})

        # An unbounded read would never ask islice for a count at all.
        assert requested, "the scan read a directory without bounding the pull"
        assert max(requested) <= 5, requested
        assert bounded is True
        assert roots == []

    def test_unreadable_entry_does_not_abort_discovery(self, tmp_path, monkeypatch):
        """An OSError from one entry probe is a skipped branch, not a 500."""
        from kiro_crew.dashboard.handlers import files as files_mod

        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / ".git").mkdir()

        class _Hostile:
            def __init__(self, entry):
                self._entry = entry
                self.name = entry.name
                self.path = entry.path

            def is_dir(self, follow_symlinks=True):
                if self.name == "a":
                    raise PermissionError(self.path)
                return self._entry.is_dir(follow_symlinks=follow_symlinks)

        real_islice = files_mod.islice

        def hostile_islice(iterable, n):
            return [_Hostile(e) for e in real_islice(iterable, n)]

        monkeypatch.setattr(files_mod, "islice", hostile_islice)
        # The probe's NOT-A-REPOSITORY verdict, spelled as git spells it. A bare
        # rc=1 with no stderr is an operational failure now, and discovery
        # refuses to scan downward from one (an outage must not be answered as
        # an empty workspace), so the stand-in has to carry the real verdict for
        # the scan under test to be reached at all.
        with patch.object(
            files_mod,
            "_run_git_bounded",
            return_value=(128, "fatal: not a git repository", False),
        ):
            files_mod._repo_scan_cache.clear()
            roots, bounded, _failed = files_mod._discover_repo_roots(str(tmp_path), ["git"], {})

        assert roots == [str(tmp_path / "b")]
        assert bounded is True

    def test_upward_root_is_normalised(self, tmp_path):
        """git spells the toplevel with forward slashes even on Windows.

        Discovery and the descendant scan must agree on separators: dispatch
        compares a root against the project dir as strings, and a row's
        ``repoRoot`` must not change shape with the route that resolved it.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        odd = f"{tmp_path}//sub/./repo"
        with patch.object(files_mod, "_run_git_bounded", return_value=(0, odd + "\n", False)):
            files_mod._repo_scan_cache.clear()
            roots, _bounded, _failed = files_mod._discover_repo_roots(str(tmp_path), ["git"], {})

        assert roots == [os.path.normpath(odd)]
        assert roots[0] == str(tmp_path / "sub" / "repo")

    @pytest.mark.asyncio
    async def test_single_repo_answer_keeps_its_shape(self, repo, mock_sel):
        (repo / "a.txt").write_text("modified\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert data.get("branch") == "trunk"
        assert "repos" not in data
        assert data["files"][0]["repoRoot"] == str(repo)
