"""Lane gating for the fork's GitHub-Releases update path.

``/api/update/fork`` predates the capability model: it answered "a release
exists whose sha differs from HEAD" as an upgrade, which on a git checkout —
the fleet's only install shape — lit an "Upgrade to 0.13.0" button for a
release OLDER than the running build, and its apply endpoint would have
``pip install --force-reinstall``ed a wheel over the editable checkout.
``_wheel_lane_refusal`` now restricts the lane to ``kirocrew``-managed venvs
and ``_release_is_newer`` orders by effective version before the sha tiebreak.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import kiro_crew
from kiro_crew.dashboard.handlers import updates_fork
from kiro_crew.platform import update_capability, update_provider


def _capability(managed_by: str, *, reason: str = ""):
    return update_capability.UpdateCapability(
        supported=True,
        managed_by=managed_by,
        mode=update_capability.MODE_NOTIFY,
        can_download=True,
        can_apply=False,
        requires_restart=True,
        unavailable_reason=reason,
    )


class TestWheelLaneRefusal:
    @pytest.mark.asyncio
    async def test_git_checkout_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            updates_fork, "derive_capability",
            lambda: _capability(update_capability.MANAGED_BY_GIT),
        )
        monkeypatch.setattr(update_provider, "resolve_provider", lambda: None)
        verdict = await updates_fork._wheel_lane_refusal()
        assert verdict is not None
        assert verdict["managed_by"] == "git"

    @pytest.mark.asyncio
    async def test_managed_venv_is_allowed(self, monkeypatch):
        monkeypatch.setattr(
            updates_fork, "derive_capability",
            lambda: _capability(update_capability.MANAGED_BY_KIROCREW),
        )
        monkeypatch.setattr(update_provider, "resolve_provider", lambda: None)
        assert await updates_fork._wheel_lane_refusal() is None

    @pytest.mark.asyncio
    async def test_policy_provider_owns_the_lane(self, monkeypatch):
        monkeypatch.setattr(
            updates_fork, "derive_capability",
            lambda: _capability(update_capability.MANAGED_BY_KIROCREW),
        )
        monkeypatch.setattr(update_provider, "resolve_provider", lambda: object())
        verdict = await updates_fork._wheel_lane_refusal()
        assert verdict is not None
        assert verdict["managed_by"] == "command"

    @pytest.mark.asyncio
    async def test_probe_failure_fails_closed(self, monkeypatch):
        def _boom():
            raise RuntimeError("git missing")

        monkeypatch.setattr(updates_fork, "derive_capability", _boom)
        verdict = await updates_fork._wheel_lane_refusal()
        assert verdict is not None
        assert verdict["managed_by"] == "unknown"


class TestReleaseIsNewer:
    """Version ordering decides; sha only breaks a same-version tie."""

    @pytest.fixture(autouse=True)
    def _pin_build(self, monkeypatch):
        monkeypatch.setattr(kiro_crew, "__version__", "0.13.0.1")
        monkeypatch.setattr(updates_fork, "_current_fork_sha", lambda: "fe68bd64")

    def test_an_older_release_is_not_an_upgrade(self):
        # The reported bug: latest release v0.13.0 vs running 0.13.0.1 used to
        # read available because the shas differ.
        available, remote_version, _ = updates_fork._release_is_newer(
            "v0.13.0+fork.g00000000"
        )
        assert available is False
        assert remote_version == "0.13.0"

    def test_a_newer_release_is_an_upgrade(self):
        for tag in ("v0.13.0.2+fork.gabcdef12", "v0.14.0+fork.gabcdef12"):
            assert updates_fork._release_is_newer(tag)[0] is True

    def test_same_version_same_sha_is_not_an_upgrade(self):
        assert updates_fork._release_is_newer("v0.13.0.1+fork.gfe68bd64")[0] is False

    def test_same_version_different_sha_is_an_upgrade(self):
        # A rebuild of the same version is a different build; only the sha
        # tells them apart, so the release stream's deliberate cuts still show.
        assert updates_fork._release_is_newer("v0.13.0.1+fork.g00000000")[0] is True

    def test_unparseable_version_falls_back_to_sha(self, monkeypatch):
        monkeypatch.setattr(kiro_crew, "__version__", "not-a-version")
        assert updates_fork._release_is_newer("v0.13.0+fork.g00000000")[0] is True
        assert updates_fork._release_is_newer("v0.13.0+fork.gfe68bd64")[0] is False


class TestApplyRestartsThroughRestartGateway:
    """A successful apply must restart via ``_restart_gateway``.

    The old code called ``reexec_python_module()`` with no arguments — that
    helper requires ``(module, args)``, so the delayed task died on a
    TypeError and the gateway kept running the pre-update code.
    """

    @pytest.mark.asyncio
    async def test_successful_apply_schedules_restart_gateway(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            updates_fork.tempfile, "mkdtemp", lambda **k: str(tmp_path)
        )
        restarted: list[object] = []

        async def _fake_restart(state):
            restarted.append(state)
            return True

        def _reexec_never(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("reexec_python_module must not be called directly")

        monkeypatch.setattr(updates_fork, "_restart_gateway", _fake_restart)
        monkeypatch.setattr(
            "kiro_crew.platform_compat.reexec_python_module", _reexec_never
        )
        monkeypatch.setattr(
            updates_fork, "require_owner_dashboard_request", lambda *a, **k: _none()
        )
        monkeypatch.setattr(updates_fork, "_wheel_lane_refusal", lambda: _none())
        monkeypatch.setattr(
            updates_fork,
            "_fetch_latest_release",
            lambda: _release(),
        )
        monkeypatch.setattr(
            updates_fork, "_release_is_newer", lambda tag: (True, "0.14.0", "abc123")
        )
        monkeypatch.setattr(
            updates_fork, "_find_wheel_url", lambda rel: "https://example/x.whl"
        )
        monkeypatch.setattr("urllib.request.urlretrieve", lambda *a, **k: None)

        class _Ok:
            returncode = 0
            stderr = ""

        monkeypatch.setattr(
            updates_fork.subprocess, "run", lambda *a, **k: _Ok()
        )
        # The delayed restart sleeps 2s before acting; skip the wait.
        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

        state = SimpleNamespace(_background_tasks=set())
        request = SimpleNamespace(app={"state": state})

        resp = await updates_fork.api_update_fork_apply(request)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["ok"] is True

        (task,) = tuple(state._background_tasks)
        await task
        assert restarted == [state]


async def _none():
    return None


async def _release():
    return {"tag_name": "v0.14.0+fork.gabc123"}
