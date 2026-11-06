"""The fork's upstream-ahead probe and fork-stamped version read.

``_check_git_checkout`` compares a checkout against its TRACKED remote — for a
fleet install that is the fork's own ``origin``, so an upstream release is
invisible until someone merges it. ``_fork_upstream_probe`` is the maintainer's
signal: when the checkout also carries an ``upstream`` remote it measures how
far the fork tip sits from upstream's default branch and reports commit counts
plus upstream's effective version. ``_version_at_sha`` reads that effective
version the way import does — a committed ``BUILD_VERSION`` stamp extends the
``__init__.py`` literal by one numeric segment — so a fork's per-push build
number reaches ``version_newer`` and the fleet auto-installs every push.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.handlers import updates
from kiro_crew import fork_version

_INIT = '__version__ = "0.13.0"\n'


def _reader(map_: dict[str, str | None]):
    """An ``_git_read`` stand-in keyed on the ``<sha>:<path>`` spec it is asked for."""

    async def _read(proj, *args, git, env, timeout=10.0):
        return map_.get(str(args[-1]))

    return _read


class TestVersionAtSha:
    """``BUILD_VERSION`` wins exactly when import would apply it."""

    @pytest.mark.asyncio
    async def test_a_valid_stamp_extends_the_literal(self, monkeypatch):
        monkeypatch.setattr(
            updates,
            "_git_read",
            _reader(
                {
                    "aaa:src/kiro_crew/__init__.py": _INIT,
                    "aaa:src/kiro_crew/BUILD_VERSION": "0.13.0.7\n",
                }
            ),
        )
        assert await updates._version_at_sha("p", "aaa", git="git", env={}) == "0.13.0.7"

    @pytest.mark.asyncio
    async def test_no_stamp_reads_the_literal(self, monkeypatch):
        monkeypatch.setattr(
            updates,
            "_git_read",
            _reader({"aaa:src/kiro_crew/__init__.py": _INIT}),
        )
        assert await updates._version_at_sha("p", "aaa", git="git", env={}) == "0.13.0"

    @pytest.mark.asyncio
    async def test_a_stamp_for_a_different_base_is_ignored(self, monkeypatch):
        """``0.14.0`` stamped over a ``0.13.0`` literal is not this build's version."""
        monkeypatch.setattr(
            updates,
            "_git_read",
            _reader(
                {
                    "aaa:src/kiro_crew/__init__.py": _INIT,
                    "aaa:src/kiro_crew/BUILD_VERSION": "0.14.0\n",
                }
            ),
        )
        assert await updates._version_at_sha("p", "aaa", git="git", env={}) == "0.13.0"

    @pytest.mark.asyncio
    async def test_a_two_segment_stamp_is_ignored(self, monkeypatch):
        """Import accepts ONE numeric segment; a second is not a build stamp."""
        monkeypatch.setattr(
            updates,
            "_git_read",
            _reader(
                {
                    "aaa:src/kiro_crew/__init__.py": _INIT,
                    "aaa:src/kiro_crew/BUILD_VERSION": "0.13.0.1.2\n",
                }
            ),
        )
        assert await updates._version_at_sha("p", "aaa", git="git", env={}) == "0.13.0"

    @pytest.mark.asyncio
    async def test_an_unreadable_init_is_empty_not_a_guess(self, monkeypatch):
        monkeypatch.setattr(updates, "_git_read", _reader({}))
        assert await updates._version_at_sha("p", "aaa", git="git", env={}) == ""


class TestForkUpstreamProbe:
    """``_fork_upstream_probe`` — advisory, and silent without an ``upstream`` remote."""

    _DEFAULTS = {
        "upstream_version": "",
        "fork_ahead_of_upstream": 0,
        "fork_behind_upstream": 0,
        "fork_sync_available": False,
    }

    @pytest.mark.asyncio
    async def test_no_upstream_remote_reports_nothing(self, monkeypatch):
        async def _read(proj, *args, git, env, timeout=10.0):
            return None  # `git config --get remote.upstream.url` unset

        monkeypatch.setattr(updates, "_git_read", _read)
        assert await updates._fork_upstream_probe("p", git="git", env={}, fork_tip="f" * 40) == self._DEFAULTS

    @pytest.mark.asyncio
    async def test_upstream_ahead_reports_counts_and_version(self, monkeypatch):
        async def _read(proj, *args, git, env, timeout=10.0):
            joined = " ".join(str(a) for a in args)
            if "remote.upstream.url" in joined:
                return "https://github.com/kirodotdev/KiroCrew.git\n"
            if "ls-remote" in joined:
                return "ref: refs/heads/main\tHEAD\n" + "u" * 40 + "\tHEAD\n"
            if "rev-list" in joined:
                return "4\t17\n"  # fork ahead 4, upstream ahead 17
            if joined.endswith("__init__.py"):
                return _INIT
            return None

        async def _fetch(*a, **k):
            return "u" * 40

        monkeypatch.setattr(updates, "_git_read", _read)
        monkeypatch.setattr(updates, "fetch_and_pin_upstream", _fetch)
        fields = await updates._fork_upstream_probe("p", git="git", env={}, fork_tip="f" * 40)
        assert fields == {
            "upstream_version": "0.13.0",
            "fork_ahead_of_upstream": 4,
            "fork_behind_upstream": 17,
            "fork_sync_available": True,
        }

    @pytest.mark.asyncio
    async def test_an_unreadable_divergence_reports_nothing(self, monkeypatch):
        async def _read(proj, *args, git, env, timeout=10.0):
            joined = " ".join(str(a) for a in args)
            if "remote.upstream.url" in joined:
                return "https://example.com/up.git\n"
            if "ls-remote" in joined:
                return "ref: refs/heads/main\tHEAD\n" + "u" * 40 + "\tHEAD\n"
            return "garbage\n"  # rev-list output that does not parse

        async def _fetch(*a, **k):
            return "u" * 40

        monkeypatch.setattr(updates, "_git_read", _read)
        monkeypatch.setattr(updates, "fetch_and_pin_upstream", _fetch)
        assert await updates._fork_upstream_probe("p", git="git", env={}, fork_tip="f" * 40) == self._DEFAULTS


class TestStatusFields:
    """``status_update_fields`` carries the probe's verdict and the fork display."""

    def test_new_fields_default_when_no_check_has_run(self):
        fields = updates.status_update_fields()
        assert fields["fork_sync_available"] is False
        assert fields["fork_behind_upstream"] == 0
        assert fields["fork_ahead_of_upstream"] == 0
        assert fields["upstream_version"] == ""

    def test_version_display_carries_the_fork_segment(self, monkeypatch):
        """``0.13.0.1+fork.g<sha>`` — official base plus the git identity."""
        monkeypatch.setattr(fork_version, "_cached", ("22e041c8", False))
        try:
            display = updates.status_update_fields()["version_display"]
        finally:
            monkeypatch.setattr(fork_version, "_cached", None)
        assert display.endswith("+fork.g22e041c8")
        assert "+fork.g22e041c8.dirty" not in display

    def test_version_display_marks_a_dirty_tree(self, monkeypatch):
        monkeypatch.setattr(fork_version, "_cached", ("22e041c8", True))
        try:
            display = updates.status_update_fields()["version_display"]
        finally:
            monkeypatch.setattr(fork_version, "_cached", None)
        assert display.endswith("+fork.g22e041c8.dirty")
