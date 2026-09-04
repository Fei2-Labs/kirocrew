"""A fork build is never offered an upstream artifact from the release feed.

The feed publishes UPSTREAM's bytes. Applying one to a fork install does not
update it, it REPLACES it -- the fork's divergence is uninstalled from a button
labelled "Update". So the feed lane reports "nothing to apply" while still
carrying ``latest_version``, and the payload says WHY.

The git-checkout lane is deliberately not covered here: it fetches the install's
own remote, which for a fork clone is the fork, so it is already correct.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import fork_version
from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform.update_capability import UpdateCapability

_CHANNEL = "stable"


def _capability() -> UpdateCapability:
    return UpdateCapability(
        supported=True,
        managed_by="kirocrew",
        mode="notify",
        can_download=True,
        can_apply=False,
        requires_restart=True,
    )


def _manifest(version: str, **extra: object) -> bytes:
    body: dict[str, object] = {
        "schema": updates._CLI_MANIFEST_SCHEMA,
        "channel": _CHANNEL,
        "version": version,
    }
    body.update(extra)
    return json.dumps(body).encode("utf-8")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    saved = dict(updates._update_info)
    fork_version.reset_cache_for_tests()
    monkeypatch.setattr(updates, "_release_channel", lambda: _CHANNEL)
    monkeypatch.setattr(
        updates, "_cdn_bases", lambda: ("https://feed.invalid", "https://cdn.invalid")
    )
    from kiro_crew.platform import wheel_engine

    monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: False)
    yield
    updates._update_info.clear()
    updates._update_info.update(saved)
    fork_version.reset_cache_for_tests()


def _run_feed_check(monkeypatch, *, remote: str, forked: bool, raw: bytes | None = None) -> dict:
    payload = raw if raw is not None else _manifest(remote)

    async def _fake(url: str):
        return 200, payload

    monkeypatch.setattr(updates, "_fetch_feed_bytes", _fake)
    monkeypatch.setattr(updates, "is_fork_build", lambda: forked)
    asyncio.run(updates._check_release_feed(_capability()))
    return updates.get_update_info()


def test_an_upstream_build_is_still_offered_the_update(monkeypatch):
    """The control: nothing about the ordinary path changes."""
    info = _run_feed_check(monkeypatch, remote="99.0.0", forked=False)
    assert info["update_available"] is True
    assert info["latest_version"] == "99.0.0"
    assert info["fork_suppressed"] is False


def test_a_fork_build_is_not_offered_the_upstream_update(monkeypatch):
    info = _run_feed_check(monkeypatch, remote="99.0.0", forked=True)
    assert info["update_available"] is False
    # Still REPORTED: the panel must be able to say what upstream moved to.
    # Withholding the version too would leave a fork user unable to see that
    # upstream released at all, which is information, not an action.
    assert info["latest_version"] == "99.0.0"
    assert info["fork_suppressed"] is True


def test_a_fork_build_with_nothing_newer_is_indistinguishable(monkeypatch):
    """Suppression must not invent a signal where there was no update anyway."""
    info = _run_feed_check(monkeypatch, remote="0.0.1", forked=True)
    assert info["update_available"] is False
    assert info["fork_suppressed"] is True


def test_the_forced_update_floor_cannot_coerce_a_fork_build(monkeypatch):
    """A signed upstream floor makes the prompt NON-DISMISSIBLE.

    The only thing it can prompt toward on this lane is the upstream artifact a
    fork build refuses, so honoring it would hold the dashboard hostage to an
    update that cannot legitimately be applied. The floor is dropped before the
    signature is even consulted.
    """

    def _must_not_verify(*args, **kwargs):  # pragma: no cover
        raise AssertionError("a fork build must drop the floor before verifying it")

    from kiro_crew.platform import feed_trust

    monkeypatch.setattr(feed_trust, "verify_manifest_signature", _must_not_verify)
    info = _run_feed_check(
        monkeypatch,
        remote="99.0.0",
        forked=True,
        raw=_manifest("99.0.0", min_version="98.0.0"),
    )
    assert "feed_min_version" not in info
    assert info["update_available"] is False


def test_the_status_payload_carries_both_halves_of_the_identity(monkeypatch):
    """ "What am I running" must answer with the base AND the fork revision."""
    monkeypatch.setattr(fork_version, "_cached", ("645d7289", True))
    fields = updates.status_update_fields()
    assert fields["upstream_base_version"] == fork_version.base_version()
    assert fields["fork_revision"] == "645d7289"
    assert fields["fork_dirty"] is True


def test_the_status_payload_is_empty_for_an_upstream_build(monkeypatch):
    monkeypatch.setattr(fork_version, "_cached", ("", False))
    fields = updates.status_update_fields()
    assert fields["fork_revision"] == ""
    assert fields["fork_dirty"] is False
    assert fields["update_fork_suppressed"] is False


def test_the_suppression_flag_reaches_the_status_payload(monkeypatch):
    _run_feed_check(monkeypatch, remote="99.0.0", forked=True)
    assert updates.status_update_fields()["update_fork_suppressed"] is True


def test_the_status_reader_never_spawns_git(monkeypatch):
    """``status_update_fields`` runs ON THE EVENT LOOP.

    Deriving the revision spawns two git processes with a five-second timeout
    each, so an unwarmed first call there would stall every session. The reader
    peeks the memo and reports "no fork revision" until the update check's
    off-loop step has filled it.
    """
    import subprocess

    monkeypatch.setattr(fork_version, "_cached", None)

    def _no_spawn(*args, **kwargs):  # pragma: no cover
        raise AssertionError("the status reader must not spawn git")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    fields = updates.status_update_fields()
    assert fields["fork_revision"] == ""
    assert fields["fork_dirty"] is False


def test_the_update_check_warms_the_revision_off_the_loop(monkeypatch):
    """The warm is what makes the About surface able to name the fork at all."""
    calls: list[str] = []
    monkeypatch.setattr(fork_version, "_cached", None)
    monkeypatch.setattr(
        fork_version, "_git_revision", lambda: (calls.append("git"), ("cafebabe", False))[1]
    )
    monkeypatch.setattr(updates, "resolve_provider", lambda: None)
    monkeypatch.setattr(
        updates,
        "derive_capability",
        lambda: UpdateCapability(
            supported=True,
            managed_by="electron",
            mode="consent",
            can_download=False,
            can_apply=False,
            requires_restart=True,
            unavailable_reason="managed-by-app",
        ),
    )
    asyncio.run(updates._do_update_check())
    assert calls == ["git"]
    assert fork_version.peek_revision() == ("cafebabe", False)
    assert updates.status_update_fields()["fork_revision"] == "cafebabe"
