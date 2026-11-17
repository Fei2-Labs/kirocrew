"""Tests for GET /api/project/git/log."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_project_git_status_log import (  # noqa: F401
    _git,
    _make_app,
    _ProbeProcess,
    _remove_tree,
    discovery_stops_at_tmp_path_fixture,
    mock_sel_fixture,
    outside_any_repo_fixture,
    passthrough_sandbox_fixture,
    repo_fixture,
    repo_template_fixture,
)

pytestmark = pytest.mark.usefixtures("passthrough_sandbox")


class TestGitLog:
    @pytest.mark.asyncio
    async def test_non_repo_returns_repo_false(self, outside_any_repo, mock_sel):
        plain = outside_any_repo
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git/log?path={plain}")
            data = await resp.json()
        assert data["repo"] is False
        assert data["commits"] == []

    @pytest.mark.asyncio
    async def test_unknown_dir_is_refused(self, repo, tmp_path, mock_sel):
        other = tmp_path / "other"
        other.mkdir()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={other}")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_returns_commits(self, repo, mock_sel):
        """Log returns at least the initial commit."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert len(data["commits"]) == 1
        c = data["commits"][0]
        assert c["message"] == "initial commit"
        assert c["author"] == "T"
        assert c["isHead"] is True
        assert "sha" in c
        assert "date" in c

    @pytest.mark.asyncio
    async def test_limit_parameter(self, repo, mock_sel):
        """limit=1 returns only 1 commit even if there are more."""
        (repo / "d.txt").write_text("x\n")
        _git(repo, "add", "d.txt")
        _git(repo, "commit", "-qm", "second commit")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}&limit=1")
            data = await resp.json()
        assert len(data["commits"]) == 1
        assert data["commits"][0]["message"] == "second commit"
        assert data["commits"][0]["isHead"] is True

    @pytest.mark.asyncio
    async def test_limit_capped_at_100(self, repo, mock_sel):
        """limit > 100 is capped to 100."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}&limit=500")
            data = await resp.json()
        # Should still work, just capped
        assert data["repo"] is True
        assert len(data["commits"]) >= 1


class TestGitLogDiscoveryBoundary:
    """The log route shares the status route's discovery classification.

    Only Git's own not-a-repository verdict (or a vanished directory) is
    ``200 {"repo": false, "commits": []}``. Every other failed discovery probe
    is ``503 git_log_unavailable``, because an empty commit list is what an
    unborn repository legitimately returns, so an outage spelled that way is
    indistinguishable from "no history yet".
    """

    _UNAVAILABLE = {
        "error": "Couldn't read the commit history.",
        "code": "git_log_unavailable",
    }

    @pytest.mark.parametrize(
        "stderr",
        (
            b"fatal: not a git repository (or any parent): .git",
            b"fatal: not a git repository \xff",
        ),
    )
    @pytest.mark.asyncio
    async def test_explicit_not_a_repository_verdict_is_repo_false(
        self, tmp_path, mock_sel, monkeypatch, stderr
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        plain = tmp_path / "plain"
        plain.mkdir()
        seen: dict[str, object] = {}

        def fake_popen(_argv, **kwargs):
            seen.update(kwargs["env"])
            seen["stdout"] = kwargs["stdout"]
            seen["stderr"] = kwargs["stderr"]
            return _ProbeProcess(128, stderr=stderr)

        monkeypatch.setattr(files_mod, "popen_limited", fake_popen)
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git/log?path={plain}")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"repo": False, "commits": []}
        # The verdict match reads Git's English diagnostic, so the probe pins
        # the C locale exactly like the status route does.
        assert seen["LC_ALL"] == "C"
        assert seen["LANGUAGE"] == "C"
        assert seen["stdout"] is subprocess.DEVNULL
        assert seen["stderr"] is subprocess.PIPE

    @pytest.mark.asyncio
    async def test_real_non_repository_is_repo_false(self, outside_any_repo, mock_sel):
        plain = outside_any_repo
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git/log?path={plain}")
            data = await resp.json()
        assert resp.status == 200
        assert data == {"repo": False, "commits": []}

    @pytest.mark.asyncio
    async def test_probe_path_text_does_not_claim_repository_absence(
        self, tmp_path, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        repo = tmp_path / "not a git repository"
        repo.mkdir()
        diagnostic = (f"fatal: detected dubious ownership in repository at '{repo}'\n").encode()
        monkeypatch.setattr(
            files_mod,
            "popen_limited",
            lambda *_args, **_kwargs: _ProbeProcess(128, stderr=diagnostic),
        )

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data == self._UNAVAILABLE

    @pytest.mark.parametrize("failure", ("sandbox", "spawn"))
    @pytest.mark.asyncio
    async def test_operational_probe_failure_is_unavailable(
        self, repo, mock_sel, monkeypatch, failure
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        if failure == "sandbox":

            def refuse(*_args, **_kwargs):
                raise RuntimeError("sandbox unavailable")

            monkeypatch.setattr(files_mod, "sandboxed_spawn_argv", refuse)
        else:
            monkeypatch.setattr(
                files_mod,
                "popen_limited",
                MagicMock(side_effect=OSError("git unavailable")),
            )

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()

        assert os.path.isdir(repo), "fixture directory unexpectedly vanished"
        assert resp.status == 503
        assert data == self._UNAVAILABLE

    @pytest.mark.asyncio
    async def test_probe_stderr_overflow_is_killed_and_unavailable(
        self, repo, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        proc = _ProbeProcess(
            128,
            stderr=b"fatal: not a git repository " + b"x" * 4096,
        )
        monkeypatch.setattr(files_mod, "popen_limited", lambda *_args, **_kwargs: proc)

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data == self._UNAVAILABLE
        assert proc.killed is True
        assert proc.waited is True

    @pytest.mark.asyncio
    async def test_probe_timeout_is_unavailable(self, repo, mock_sel, monkeypatch):
        from kiro_crew.dashboard.handlers import files as files_mod

        real_run = files_mod._run_git_bounded

        def killed_probe(args, **kwargs):
            if args[-2:] == ["rev-parse", "--git-dir"]:
                return -9, "", True
            return real_run(args, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", killed_probe)

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data == self._UNAVAILABLE

    @pytest.mark.asyncio
    async def test_corrupt_repository_config_is_unavailable(
        self, repo, mock_sel, discovery_stops_at_tmp_path
    ):
        # Real Git, no mocks: a malformed ``.git/config`` makes discovery fail
        # with ``fatal: bad config line`` -- a nonzero probe that is NOT the
        # not-a-repository verdict, so it is an outage rather than absence.
        (repo / ".git" / "config").write_text("[core\nbogus\n")
        probe = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"},
        )
        assert probe.returncode != 0
        assert "not a git repository" not in probe.stderr.lower()

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()

        assert resp.status == 503
        assert data == self._UNAVAILABLE

    @pytest.mark.asyncio
    async def test_unborn_repository_is_a_repository_with_no_commits(
        self, tmp_path, mock_sel, discovery_stops_at_tmp_path
    ):
        # Discovery succeeds and only ``git log`` fails (no HEAD yet): that is
        # the genuinely empty history, kept distinct from the discovery outage.
        project = tmp_path / "unborn"
        project.mkdir()
        _git(project, "init", "-q", "-b", "trunk")

        async with TestClient(TestServer(_make_app(str(project)))) as client:
            resp = await client.get(f"/api/project/git/log?path={project}")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"repo": True, "commits": []}

    @pytest.mark.asyncio
    async def test_vanished_directory_after_probe_failure_is_repo_false(
        self, repo, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        def vanish_then_fail(*_args, **_kwargs):
            _remove_tree(repo)
            raise FileNotFoundError("cwd vanished")

        monkeypatch.setattr(files_mod, "popen_limited", vanish_then_fail)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"repo": False, "commits": []}

    def test_verdict_predicate_is_shared_by_both_routes(self):
        from kiro_crew.dashboard.handlers import files as files_mod

        assert files_mod._is_not_a_repo_verdict(
            "fatal: not a git repository (or any parent): .git\n"
        )
        assert files_mod._is_not_a_repo_verdict(
            "warning: something\n  FATAL: NOT A GIT REPOSITORY: /x\n"
        )
        assert not files_mod._is_not_a_repo_verdict("")
        assert not files_mod._is_not_a_repo_verdict(
            "fatal: detected dubious ownership in repository at '/not a git repository'"
        )
        assert not files_mod._is_not_a_repo_verdict("fatal: bad config line 1 in file .git/config")
