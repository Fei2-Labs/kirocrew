"""Fork identity: the git-derived local version segment and its blast radius.

The fork's ``__version__`` is upstream's literal, so on its own it cannot tell a
fork build apart from the upstream build it forked. ``fork_version`` adds a PEP
440 local segment for that, and these tests pin the two properties that make the
segment safe to carry: it never changes a version's CLASSIFICATION, and it never
changes a version's ORDER.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from kiro_crew import fork_version
from kiro_crew.changelog import base_version as changelog_base
from kiro_crew.changelog import running_release
from kiro_crew.dashboard.handlers.updates import _is_newer, _version_key
from kiro_crew.release_channel import channel, is_prerelease


@pytest.fixture(autouse=True)
def _clear_cache():
    """The revision is memoized for the process; every test starts cold."""
    fork_version.reset_cache_for_tests()
    yield
    fork_version.reset_cache_for_tests()


# --------------------------------------------------------------------------
# strip_local
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.5.0+fork.g645d7289", "0.5.0"),
        ("0.5.0+fork.gb1234ab.dirty", "0.5.0"),
        ("0.4.0-rc.9+fork.g645d7289", "0.4.0-rc.9"),
        ("0.4.0rc9", "0.4.0rc9"),
        ("0.4.0", "0.4.0"),
        ("  0.4.0+x  ", "0.4.0"),
        ("", ""),
        ("+fork.gdeadbeef", ""),
    ],
)
def test_strip_local_removes_only_the_local_segment(raw, expected):
    assert fork_version.strip_local(raw) == expected


# --------------------------------------------------------------------------
# HAZARD 1: the unanchored prerelease matcher vs a hex sha
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version",
    [
        # No a/b/rc-plus-digit anywhere in the sha.
        "0.5.0+fork.g645d7289",
        # "b1234" -- the pre-strip matcher read this as PEP 440 prerelease b1234.
        "0.5.0+fork.gb1234ab",
        # "a1" -- same failure, different hash.
        "0.5.0+fork.ga1b2c3d",
        # Both letters, plus the dirty marker.
        "0.5.0+fork.gab12cd34.dirty",
    ],
)
def test_stable_base_stays_stable_whatever_the_sha_spells(version):
    """A stable base must classify ``stable`` for EVERY commit hash.

    Before the strip, roughly 46% of eight-character shas contain an ``a`` or
    ``b`` followed by a decimal digit, and the unanchored ``(?:a|b|rc)\\d+``
    matcher read that as a prerelease segment -- so a fork build reported
    ``insider`` DEPENDING ON THE COMMIT HASH. That fed the bug-report channel
    label, ``is_prerelease()``, and the dashboard.
    """
    assert channel(version) == "stable"
    assert is_prerelease(version) is False


def test_the_bare_matcher_is_still_the_trap_this_documents():
    """Guards the reason the strip exists, not the strip itself.

    If ``_PEP440_PRERELEASE`` is ever anchored, this fails and the comment
    telling the next reader why every caller strips can be simplified. Until
    then, the matcher genuinely does misread a sha and the strip is load-bearing.
    """
    from kiro_crew.release_channel import _PEP440_PRERELEASE

    assert _PEP440_PRERELEASE.search("0.5.0+fork.gb1234ab") is not None


@pytest.mark.parametrize(
    "version,expected",
    [
        ("0.4.0-rc.9+fork.g645d7289", "insider"),
        ("0.4.0rc9+fork.gb1234ab", "insider"),
        ("0.4.0.dev20260101+fork.ga1b2c3d", "nightly"),
        ("0.4.0-nightly.20260101t000000+fork.gb1", "nightly"),
    ],
)
def test_a_real_prerelease_base_keeps_its_channel(version, expected):
    """The strip must not swing the other way and hide a real prerelease."""
    assert channel(version) == expected


# --------------------------------------------------------------------------
# HAZARD 2: ordering
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sha", ["g645d7289", "gb1234ab", "ga1b2c3d"])
def test_a_local_segment_never_changes_the_ordering_key(sha):
    assert _version_key(f"0.5.0+fork.{sha}") == _version_key("0.5.0")
    assert _version_key(f"0.5.0+fork.{sha}.dirty") == _version_key("0.5.0")


def test_a_fork_build_is_neither_newer_nor_older_than_its_own_base():
    """The regression that would have offered a same-release "update".

    ``_PEP440_RE`` is anchored, so an unstripped ``0.5.0+fork.gb1234ab`` fell
    through to the semver branch, which read the FIRST integer out of the sha and
    ranked the build as prerelease 1234 of ``0.5.0`` -- below the bare release.
    Upstream's identical ``0.5.0`` then compared as newer, and applying it would
    have replaced the fork.
    """
    forked = "0.5.0+fork.gb1234ab"
    assert _is_newer("0.5.0", forked) is False
    assert _is_newer(forked, "0.5.0") is False
    # A genuinely newer upstream release still compares newer.
    assert _is_newer("0.5.1", forked) is True
    assert _is_newer("0.4.9", forked) is False


# --------------------------------------------------------------------------
# HAZARD 4: the changelog parsers
# --------------------------------------------------------------------------


def test_the_changelog_parsers_fold_a_local_segment_onto_its_base():
    """A fork build must not open a second, apparently-unshipped row."""
    forked = "0.4.0-rc.9+fork.g645d7289.dirty"
    assert changelog_base(forked) == "0.4.0"
    assert running_release(forked) == ("0.4.0", True)
    assert running_release("0.4.0+fork.gb1234ab") == ("0.4.0", False)


# --------------------------------------------------------------------------
# Derivation: baked, live git, and neither
# --------------------------------------------------------------------------


def _bake(monkeypatch, **attrs) -> None:
    module = types.ModuleType("kiro_crew._build_info")
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, "kiro_crew._build_info", module)


def test_a_baked_revision_wins_and_never_spawns_git(monkeypatch):
    _bake(monkeypatch, FORK_STAMPED=True, FORK_REVISION="deadbeef", FORK_DIRTY=False)

    def _no_spawn(*args, **kwargs):  # pragma: no cover - asserts it is not reached
        raise AssertionError("git must not be spawned when a revision is baked")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    assert fork_version.fork_revision() == ("deadbeef", False)
    assert fork_version.local_segment() == "+fork.gdeadbeef"


def test_a_baked_dirty_flag_reaches_the_version_string(monkeypatch):
    _bake(monkeypatch, FORK_STAMPED=True, FORK_REVISION="deadbeef", FORK_DIRTY=True)
    assert fork_version.local_segment() == "+fork.gdeadbeef.dirty"


def test_an_empty_baked_revision_is_terminal(monkeypatch):
    """A stamped build with no revision must not fall through to a git spawn."""
    _bake(monkeypatch, FORK_STAMPED=True, FORK_REVISION="", FORK_DIRTY=False)

    def _no_spawn(*args, **kwargs):  # pragma: no cover
        raise AssertionError("git must not be spawned for a stamped build")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    assert fork_version.fork_revision() == ("", False)
    assert fork_version.is_fork_build() is False
    assert fork_version.full_version() == fork_version.base_version()


def test_a_baked_non_hex_revision_is_refused(monkeypatch):
    _bake(monkeypatch, FORK_STAMPED=True, FORK_REVISION="not a sha", FORK_DIRTY=False)
    assert fork_version.fork_revision() == ("", False)


def test_an_unstamped_build_info_falls_through_to_git(monkeypatch):
    """A wheel released before FORK_STAMPED existed still carries DISTRIBUTION."""
    _bake(monkeypatch, DISTRIBUTION="wheel")
    monkeypatch.setattr(fork_version, "_git_revision", lambda: ("cafebabe", False))
    assert fork_version.fork_revision() == ("cafebabe", False)


def test_no_git_binary_degrades_silently(monkeypatch):
    from kiro_crew import platform_compat

    _bake(monkeypatch, DISTRIBUTION="wheel")
    monkeypatch.setattr(platform_compat, "trusted_git_bin", lambda: None)
    assert fork_version.fork_revision() == ("", False)
    assert fork_version.is_fork_build() is False
    # The plain base version, never a placeholder: a fabricated "+fork.unknown"
    # would ASSERT a fork build on an install with no evidence of one.
    assert fork_version.full_version() == fork_version.base_version()
    assert "+" not in fork_version.full_version()


def test_a_failing_git_call_degrades_silently(monkeypatch, tmp_path):
    _bake(monkeypatch, DISTRIBUTION="wheel")
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "trusted_git_bin", lambda: "/usr/bin/git")
    monkeypatch.setattr(fork_version, "_repo_root", lambda: str(tmp_path))

    def _fail(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", _fail)
    assert fork_version.fork_revision() == ("", False)


def test_a_git_timeout_degrades_silently(monkeypatch):
    _bake(monkeypatch, DISTRIBUTION="wheel")
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "trusted_git_bin", lambda: "/usr/bin/git")

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert fork_version.fork_revision() == ("", False)


def test_a_real_checkout_derives_a_live_revision(monkeypatch, tmp_path):
    """The source-checkout path, against a real repository."""
    from kiro_crew import platform_compat

    git = platform_compat.trusted_git_bin()
    if git is None:
        pytest.skip("no trusted git binary on this host")

    run = [git, "-C", str(tmp_path)]
    subprocess.run([git, "init", "-q", str(tmp_path)], check=True)
    subprocess.run([*run, "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run([*run, "config", "user.name", "t"], check=True)
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run([*run, "add", "a.txt"], check=True)
    subprocess.run([*run, "-c", "commit.gpgsign=false", "commit", "-qm", "x"], check=True)

    _bake(monkeypatch, DISTRIBUTION="wheel")
    monkeypatch.setattr(fork_version, "_repo_root", lambda: str(tmp_path))

    revision, dirty = fork_version.fork_revision()
    assert len(revision) == fork_version.REVISION_WIDTH
    assert all(c in "0123456789abcdef" for c in revision)
    assert dirty is False
    assert fork_version.local_segment() == f"+fork.g{revision}"

    # A dirty tree gets the marker.
    fork_version.reset_cache_for_tests()
    (tmp_path / "a.txt").write_text("b\n", encoding="utf-8")
    revision2, dirty2 = fork_version.fork_revision()
    assert (revision2, dirty2) == (revision, True)
    assert fork_version.local_segment() == f"+fork.g{revision}.dirty"


def test_a_non_repository_directory_is_not_a_fork_build(monkeypatch, tmp_path):
    from kiro_crew import platform_compat

    if platform_compat.trusted_git_bin() is None:
        pytest.skip("no trusted git binary on this host")
    _bake(monkeypatch, DISTRIBUTION="wheel")
    monkeypatch.setattr(fork_version, "_repo_root", lambda: str(tmp_path))
    assert fork_version.fork_revision() == ("", False)


def test_the_revision_is_derived_once_per_process(monkeypatch):
    calls: list[int] = []

    def _once():
        calls.append(1)
        return "cafebabe", False

    _bake(monkeypatch, DISTRIBUTION="wheel")
    monkeypatch.setattr(fork_version, "_git_revision", _once)
    for _ in range(5):
        fork_version.fork_revision()
        fork_version.local_segment()
        fork_version.full_version()
    assert len(calls) == 1


# --------------------------------------------------------------------------
# The composed version string
# --------------------------------------------------------------------------


def test_the_full_version_is_the_base_plus_the_segment(monkeypatch):
    _bake(monkeypatch, FORK_STAMPED=True, FORK_REVISION="645d7289", FORK_DIRTY=False)
    from kiro_crew import __version__

    assert fork_version.base_version() == __version__
    assert fork_version.full_version() == f"{__version__}+fork.g645d7289"
    # The whole point: the fork half round-trips off for every comparison.
    assert fork_version.strip_local(fork_version.full_version()) == __version__
    # ...and classification is unchanged by it.
    assert channel(fork_version.full_version()) == channel(__version__)


def test_full_version_honors_a_caller_supplied_base(monkeypatch):
    """A caller passes ITS OWN module binding of ``__version__``.

    ``diagnostics`` and ``cli_server`` are tested by monkeypatching their module
    binding, so a ``full_version()`` that reached for the package's canonical
    value would silently ignore the patch and report the real build's version
    inside a test that believed it had pinned another. That is not theoretical:
    it broke `test_diagnostics.py::test_issue_url_prefills_the_channel_dropdown`
    and both of `test_update_wheel_dispatch.py`'s CLI tests.
    """
    _bake(monkeypatch, FORK_STAMPED=True, FORK_REVISION="645d7289", FORK_DIRTY=False)
    assert fork_version.full_version("0.1.4-nightly.20260807t061500") == (
        "0.1.4-nightly.20260807t061500+fork.g645d7289"
    )
    # Idempotent: passing an already-composed version does not double the segment.
    composed = fork_version.full_version("0.9.9")
    assert fork_version.full_version(composed) == composed


def test_base_version_matches_the_declared_literal():
    """``__version__`` stays UPSTREAM's bare literal.

    The fork identity rides a local segment precisely so this stays true: the
    changelog gate, the promotion contract, the release feed comparison and the
    desktop updater's compare gate all key on this value.
    """
    from kiro_crew import __version__

    assert "+" not in __version__
    assert fork_version.base_version() == __version__
