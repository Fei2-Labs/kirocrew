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

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates_fork")

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


async def api_update_fork_check(request: web.Request) -> web.Response:
    """GET /api/update/fork — check for a newer fork release."""
    try:
        release = await _fetch_latest_release()
    except Exception as exc:
        logger.warning("fork update check failed: %s", exc)
        return web.json_response({"available": False, "error": str(exc)}, status=502)

    tag = release.get("tag_name", "")
    parsed = _parse_tag(tag)
    if not parsed:
        return web.json_response({"available": False, "error": f"unrecognized tag: {tag}"})

    remote_version, remote_sha = parsed
    current_sha = _current_fork_sha()
    current_version = kiro_crew.__version__

    available = remote_sha != current_sha
    return web.json_response({
        "available": available,
        "current_version": current_version,
        "current_sha": current_sha,
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

    try:
        release = await _fetch_latest_release()
    except Exception as exc:
        logger.error("fork update: cannot fetch release: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    wheel_url = _find_wheel_url(release)
    if not wheel_url:
        return web.json_response(
            {"ok": False, "error": "no wheel asset in latest release"}, status=404
        )

    tag = release.get("tag_name", "")
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

    # Schedule a restart after responding — give the response time to reach the client.
    async def _delayed_restart() -> None:
        await asyncio.sleep(2)
        logger.info("fork update: restarting gateway")
        from kiro_crew.platform_compat import reexec_python_module
        reexec_python_module()

    asyncio.get_event_loop().create_task(_delayed_restart())

    return web.json_response({
        "ok": True,
        "installed_tag": tag,
        "message": "Installed. Gateway restarting in 2 seconds.",
    })
