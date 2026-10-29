"""Cached views of the claude-agent-acp resolver.

Doctor and the dashboard's install probe ask "is it installed" on every probe;
the spawn arm asks per session. These wrappers expose the resolver through the
ONE cache the spawn arm already keeps in :mod:`kiro_crew.acp.harness.claude`,
so all three name the same directories when they answer, and an install takes
effect when that cache is reset rather than silently on the next probe.
"""

from __future__ import annotations

from kiro_crew.acp.harness import claude as _claude_harness


def _resolve_claude_acp_bin_cached_full() -> tuple[list[str] | None, str]:
    """``_resolve_claude_acp_bin`` memoised, argv WITH the PATH it was found on.

    BOTH outcomes are cached, failure included, and that is the point: the
    "not found" message names the directories actually walked rather than
    directories from a later, different reading of PATH. Re-resolving on a miss
    would let a PATH change landing in that window report a search that never
    happened. An install therefore takes effect once the cache is reset — the
    install endpoint and doctor both do that — not silently on the next spawn.

    Doctor and the install probe share this cache with spawn so they do not
    re-walk the ladder.
    """
    cache = _claude_harness._claude_acp_argv_cache
    if isinstance(cache, tuple):
        return cache
    resolved = _claude_harness._resolve_claude_acp_bin()
    _claude_harness._claude_acp_argv_cache = resolved
    return resolved


def _resolve_claude_acp_bin_cached() -> list[str] | None:
    """The argv half of ``_resolve_claude_acp_bin_cached_full``.

    Doctor and the dashboard install probe want only "is it installed"; the
    search path is for the spawn arm's error message.
    """
    return _resolve_claude_acp_bin_cached_full()[0]
