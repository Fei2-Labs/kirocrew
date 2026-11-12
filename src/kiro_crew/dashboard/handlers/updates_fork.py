"""Check for and apply fork updates from GitHub Releases.

GET  /api/update/fork  — check whether a newer fork release exists.
POST /api/update/fork  — download the latest wheel and install it, then restart.

The check hits the GitHub Releases API for Fei2-Labs/kirocrew and compares the
latest tag's embedded version against the running one.  The apply downloads the
wheel asset, installs it with pip, and restarts the gateway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web

import kiro_crew
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.updates_apply import _restart_gateway
from kiro_crew.dashboard.handlers.updates_verdict import _is_newer
from kiro_crew.platform.update_capability import MANAGED_BY_KIROCREW, derive_capability

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")

FORK_REPO = "Fei2-Labs/kirocrew"
RELEASES_API = f"https://api.github.com/repos/{FORK_REPO}/releases/latest"

# Matches "v0.11.0+fork.gabcdef12" → version="0.11.0", sha="abcdef12"
_TAG_RE = re.compile(r"^v(?P<version>[^+]+)\+fork\.g(?P<sha>[0-9a-f]+)$")


def _parse_tag(tag: str) -> tuple[str, str] | None:
    """Extract (version, short_sha) from a fork release tag."""
    m = _TAG_RE.match(tag)
    return (m.group("version"), m.group("sha")) if m else None


async def _fetch_latest_release() -> dict[str, Any]:
    """Fetch the latest GitHub Release metadata (off the event loop)."""
    import urllib.request
    import json

    def _get() -> dict[str, Any]:
        req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())  # type: ignore[no-any-return]

    return await asyncio.to_thread(_get)


def _find_wheel_url(release: dict[str, Any]) -> str | None:
    """Return the browser_download_url of the first .whl asset, or None."""
    for asset in release.get("assets", []):
        if asset.get("name", "").endswith(".whl"):
            return asset["browser_download_url"]  # type: ignore[no-any-return]
    return None


def _current_fork_sha() -> str:
    """The short sha of this build's fork revision, or empty string."""
    try:
        from kiro_crew._build_info import FORK_REVISION
        return FORK_REVISION or ""
    except ImportError:
        return ""


async def _wheel_lane_refusal() -> dict[str, Any] | None:
    """None when the release/wheel lane may serve this install, else the reason.

    Only a ``kirocrew``-managed venv can have its bytes replaced by
    ``pip install --force-reinstall``: a git checkout updates through the git
    lane (fetch + reset + rebuild), an Electron bundle through its own updater,
    a container through an image pull, and a policy ``updates`` provider owns
    whatever it governs. The releases feed is the fallback lane for plain
    pip-managed installs — everywhere else it is a wrong answer at best and a
    clobbered checkout at worst. Both probes shell out or read pinned policy,
    so they run off the event loop together.
    """
    def _probe() -> tuple[Any, Any]:
        from kiro_crew.platform.update_provider import resolve_provider
        return derive_capability(), resolve_provider()

    try:
        capability, provider = await asyncio.get_running_loop().run_in_executor(None, _probe)
    except Exception:
        # Fail closed: an install whose lane cannot be identified must not be
        # offered a wheel that could clobber a checkout or bundle it sits in.
        logger.warning("fork update: capability probe failed", exc_info=True)
        return {"available": False, "managed_by": "unknown"}
    if provider is not None:
        return {"available": False, "managed_by": "command"}
    if capability.managed_by == MANAGED_BY_KIROCREW:
        return None
    return {
        "available": False,
        "managed_by": capability.managed_by,
        "unavailable_reason": capability.unavailable_reason or "",
    }


def _release_is_newer(tag: str) -> tuple[bool, str, str]:
    """(available, remote_version, remote_sha) for a release tag.

    Ordering, not inequality: a release OLDER than this build must never read
    as an upgrade — the tag's sha differs from HEAD on every check either way,
    which is what used to light the button on an up-to-date install.
    Same-version different-sha still counts: the fork cuts rebuilds
    deliberately, and only the sha can tell those builds apart.
    """
    parsed = _parse_tag(tag)
    if not parsed:
        return False, "", ""
    remote_version, remote_sha = parsed
    current_version = kiro_crew.__version__

    newer = _is_newer(remote_version, current_version)
    if newer is None:
        available = remote_sha != _current_fork_sha()
    elif newer:
        available = True
    else:
        same_version = _is_newer(current_version, remote_version) is False
        available = same_version and remote_sha != _current_fork_sha()
    return available, remote_version, remote_sha


async def api_update_fork_check(request: web.Request) -> web.Response:
    """GET /api/update/fork — check for a newer fork release."""
    refusal = await _wheel_lane_refusal()
    if refusal is not None:
        return web.json_response(refusal)

    try:
        release = await _fetch_latest_release()
    except Exception as exc:
        logger.warning("fork update check failed: %s", exc)
        return web.json_response({"available": False, "error": str(exc)}, status=502)

    tag = release.get("tag_name", "")
    if not _parse_tag(tag):
        return web.json_response({"available": False, "error": f"unrecognized tag: {tag}"})

    available, remote_version, remote_sha = _release_is_newer(tag)
    return web.json_response({
        "available": available,
        "current_version": kiro_crew.__version__,
        "current_sha": _current_fork_sha(),
        "remote_version": remote_version,
        "remote_sha": remote_sha,
        "remote_tag": tag,
        "release_url": release.get("html_url", ""),
    })


async def api_update_fork_apply(request: web.Request) -> web.Response:
    """POST /api/update/fork — download latest wheel and install it."""
    # Owner gate — only the dashboard owner can trigger an upgrade.
    refused = await require_owner_dashboard_request(request, "update_fork")
    if refused is not None:
        return refused

    refusal = await _wheel_lane_refusal()
    if refusal is not None:
        return web.json_response(
            {"ok": False, "error": f"release-lane update refused: managed_by={refusal['managed_by']}"},
            status=409,
        )

    try:
        release = await _fetch_latest_release()
    except Exception as exc:
        logger.error("fork update: cannot fetch release: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    tag = release.get("tag_name", "")
    available, _, _ = _release_is_newer(tag)
    if not available:
        # Same ordering gate as the check: a direct POST must not install a
        # release the check would have refused as older-or-equal.
        return web.json_response(
            {"ok": False, "error": f"latest release {tag or '?'} is not newer than this build"},
            status=409,
        )

    wheel_url = _find_wheel_url(release)
    if not wheel_url:
        return web.json_response(
            {"ok": False, "error": "no wheel asset in latest release"}, status=404
        )

    logger.info("fork update: applying %s from %s", tag, wheel_url)

    def _download_and_install() -> str:
        """Download wheel + pip install. Runs off the event loop."""
        import urllib.request

        tmpdir = tempfile.mkdtemp(prefix="kirocrew-fork-update-")
        wheel_name = wheel_url.rsplit("/", 1)[-1]
        wheel_path = os.path.join(tmpdir, wheel_name)

        # Download
        urllib.request.urlretrieve(wheel_url, wheel_path)

        # Find the Python that runs this gateway — same interpreter, same site.
        python = sys.executable

        # Install
        cmd = [
            python, "-m", "pip", "install",
            "--break-system-packages",
            "--force-reinstall", "--no-deps",
            wheel_path,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, encoding="utf-8"
            )
        except subprocess.TimeoutExpired:
            return "pip install timed out after 120s"

        if result.returncode != 0:
            # Retry without --break-system-packages (non-uv envs)
            cmd.remove("--break-system-packages")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, encoding="utf-8"
            )
            if result.returncode != 0:
                return f"pip install failed: {result.stderr[-500:]}"

        return ""

    error = await asyncio.to_thread(_download_and_install)
    if error:
        logger.error("fork update install failed: %s", error)
        return web.json_response({"ok": False, "error": error}, status=500)

    logger.info("fork update: installed %s, scheduling restart", tag)

    # Restart through _restart_gateway, the same path POST /api/restart takes:
    # reexec_python_module() takes required args, so calling it bare raised
    # TypeError in the task and the gateway stayed up on the old code.
    state = request.app["state"]

    async def _delayed_restart() -> None:
        # Reply BEFORE restarting — the exec replaces the process image.
        await asyncio.sleep(2)
        logger.info("fork update: restarting gateway")
        try:
            await _restart_gateway(state)
        except Exception:
            logger.exception("fork update: gateway restart failed")

    task = asyncio.create_task(_delayed_restart())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)

    return web.json_response({
        "ok": True,
        "installed_tag": tag,
        "message": "Installed. Gateway restarting in 2 seconds.",
    })
