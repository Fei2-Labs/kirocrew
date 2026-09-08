"""KEYSTONE operator opt-ins that LOOSEN an agent restriction for normal work.

State lives in ``<config_dir>/workflow_policy.json`` — **not** in ``config.json``.
Same security reasoning, and the same precedent, as ``computer_use.json`` and
``denied_commands.json``: a control whose OPEN position widens what the agent may
do is a security ceiling, not a preference, so it must sit somewhere a
prompt-injected or auto-approved agent shell cannot reach. The leaf is on
``security._CREW_SECRET_LEAVES``, so ``is_sensitive_path`` blocks agent reads and
writes on the tool path and ``is_sensitive_bash_command`` blocks the shell forms
(``cat``, ``>``, ``tee``, archive extraction into the trust root). The only writer
is the dashboard handler, which does not route through the agent tool gate.

Three INDEPENDENT opt-ins live here. They are not a general "overrides" bucket:
each is a named decision with its own default, its own failure direction, and its
own reason for existing. Do not add a fourth without the same treatment.

``git_publication_mode``
    ``"protected"`` (default) keeps Kiro Crew's built-in git-publication floor:
    a push naming no branch, or naming a protected branch, is denied before the
    configurable rule tiers run.

    ``"repository_governed"`` says the operator relies on the REMOTE HOST's
    controls — GitHub Rulesets, branch protection, required reviews and checks —
    as the authoritative publication boundary, and Kiro Crew stops applying its
    own branch-name parsing. Nothing else changes: credential protection,
    sensitive-path protection, self-protection and the destructive-command rules
    are untouched, and the push is still audited exactly as an allowed push is.

    Why the mode exists at all: branch names are not a global security boundary.
    A repository that already defines its publication controls at the remote gets
    path-dependent behaviour instead of protection — an ordinary ``git push`` is
    refused while the equivalent ref update through a host API is not, so the
    effective boundary becomes the command's spelling rather than the capability.

``forward_ssh_agent``
    ``False`` (default) keeps ``SSH_AUTH_SOCK`` scrubbed from the sandboxed agent
    environment, so agent-run ``ssh`` and ``git`` cannot reach the operator's
    running ssh-agent.

    ``True`` forwards it. This is a REAL widening and the docstring will not soften
    it: every key the agent socket holds becomes usable by anything running inside
    the sandbox, for as long as the agent is unlocked. It exists because signed
    commits (``commit.gpgsign=true`` with ``gpg.format=ssh``) and ssh remotes are
    otherwise impossible from inside the sandbox — the agent finds no askpass and
    the commit simply fails.

``allow_external_handoff``
    ``False`` (default) refuses ``POST /api/chat/handoff`` and its
    ``session_handoff`` MCP tool: an external agent cannot make this host start
    an unattended Kiro Crew run.

    ``True`` permits it. What that opens is narrow but real: any local process
    that can read the gateway's own secret — which is every process running as
    this user — can then create a chat session and START a turn in it, without a
    human in the loop at that moment. It is a separate decision from installing
    the MCP server, because installing a read-only tool server and authorising
    autonomous runs are not the same consent.

    What it deliberately does NOT open: a handed-over session gains no approval
    authority. It runs under the grants the operator already made (slot trust,
    YOLO) and stops at the first tool those do not cover, exactly like any other
    unattended slot. Auto-approval sources stay slot trust or YOLO only — see the
    ``auth-grant-sources`` divergence, which removed ``approval_mode: "auto"`` as
    an unconditional grant. Opening this flag must never become a fourth source.

Every read fails soft to the SAFE position — ``protected``, no forwarding, no
handoff — for the same reason ``computer_use.load_state`` fails soft to disabled:
a missing, truncated or hand-mangled ceiling file must never be read generously.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import loader as config_loader

logger = logging.getLogger(__name__)

#: Leaf name under the config dir. Named in ``security._CREW_SECRET_LEAVES``.
STATE_FILE_NAME = "workflow_policy.json"

#: Owner-only: the file records security decisions about this host.
_STATE_FILE_MODE = 0o600

#: Key holding the git-publication mode.
KEY_GIT_PUBLICATION_MODE = "git_publication_mode"

#: The default mode — Kiro Crew's own publication floor applies.
GIT_PUBLICATION_PROTECTED = "protected"

#: The opt-in mode — the repository host's controls are authoritative.
GIT_PUBLICATION_REPOSITORY_GOVERNED = "repository_governed"

#: Every legal spelling of the mode. A value outside this set reads as the
#: default, because an unrecognised ceiling value is a mistake and the safe
#: reading of a mistake is the restrictive one.
GIT_PUBLICATION_MODES: frozenset[str] = frozenset(
    {GIT_PUBLICATION_PROTECTED, GIT_PUBLICATION_REPOSITORY_GOVERNED}
)

#: Key holding the ssh-agent forwarding opt-in.
KEY_FORWARD_SSH_AGENT = "forward_ssh_agent"

#: Key holding the external-handoff opt-in.
KEY_ALLOW_EXTERNAL_HANDOFF = "allow_external_handoff"


def workflow_policy_path() -> Path:
    """Path to the keystone ``workflow_policy.json``.

    Delegates to ``config.loader.workflow_policy_path()`` when that helper is
    present so there is a single canonical definition (and so the suite's usual
    ``patch("kiro_crew.config.loader.…")`` redirection keeps working). The
    fallback computes the same path from ``config_dir()`` — resolved through the
    loader module attribute, not a direct import, so a test that patches
    ``config.loader.config_dir`` still redirects us.
    """
    dedicated = getattr(config_loader, "workflow_policy_path", None)
    if callable(dedicated):
        path = dedicated()
        return path if isinstance(path, Path) else Path(path)
    return config_loader.config_dir() / STATE_FILE_NAME


def load_state() -> dict:
    """Read the keystone state (fail-soft to ``{}``).

    ``{}`` is the safe position for both opt-ins: the publication floor applies
    and the ssh agent is not forwarded.
    """
    try:
        raw = json.loads(workflow_policy_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("workflow_policy.json load failed; using safe defaults", exc_info=True)
        return {}


def git_publication_mode(state: "dict | None" = None) -> str:
    """The active publication mode, always one of :data:`GIT_PUBLICATION_MODES`.

    Anything that is not the exact ``"repository_governed"`` string — a missing
    key, a bool, a differently-cased spelling, a typo — reads as
    ``"protected"``. Strict equality rather than a normalising parse for the same
    reason ``computer_use.is_enabled`` tests ``is True``: the only spelling that
    should widen a ceiling is the one the dashboard writes, and a generous parser
    is how a hand-edited file ends up meaning something its author did not type.

    *state* lets a caller that already loaded the file avoid a second read.
    """
    data = load_state() if state is None else state
    value = data.get(KEY_GIT_PUBLICATION_MODE)
    if value == GIT_PUBLICATION_REPOSITORY_GOVERNED:
        return GIT_PUBLICATION_REPOSITORY_GOVERNED
    return GIT_PUBLICATION_PROTECTED


def git_publication_floor_applies(state: "dict | None" = None) -> bool:
    """Whether Kiro Crew's own git-publication floor should deny.

    The predicate the enforcement path calls, rather than a mode-string
    comparison at the call site: the floor is security-critical, and a bare
    ``!= "protected"`` there would fail toward ALLOW the moment a third mode is
    added. Named positively for the restrictive answer.
    """
    return git_publication_mode(state) == GIT_PUBLICATION_PROTECTED


def forward_ssh_agent(state: "dict | None" = None) -> bool:
    """Whether ``SSH_AUTH_SOCK`` may reach the sandboxed agent environment.

    Strict identity against ``True``: a hand-edited ``"true"`` string is truthy
    in Python and must not hand the agent the operator's signing keys.
    """
    data = load_state() if state is None else state
    return data.get(KEY_FORWARD_SSH_AGENT) is True


def allow_external_handoff(state: "dict | None" = None) -> bool:
    """Whether an external agent may start an unattended session on this host.

    Strict identity against ``True``, like the other two: a hand-edited
    ``"true"`` string is truthy in Python and must not authorise autonomous runs
    launched by anything that can read a local secret.
    """
    data = load_state() if state is None else state
    return data.get(KEY_ALLOW_EXTERNAL_HANDOFF) is True


def save_state(state: dict) -> None:
    """Write the keystone state atomically, owner-only.

    Callers must pass the COMPLETE object (read-modify-write): this replaces the
    file wholesale, and the dashboard handler is responsible for refusing to
    mutate a corrupt file rather than clobbering it — resetting a populated but
    unparseable ceiling to defaults would be a silent security change in whichever
    direction the operator did not choose.

    Raises on failure (``OSError``) so the HTTP handler can report a real error;
    the read path is the only fail-soft direction here.
    """
    if not isinstance(state, dict):
        raise ValueError("workflow_policy.json state must be a dict")
    mode = state.get(KEY_GIT_PUBLICATION_MODE, GIT_PUBLICATION_PROTECTED)
    if mode not in GIT_PUBLICATION_MODES:
        raise ValueError(f"unknown git_publication_mode: {mode!r}")
    forward = state.get(KEY_FORWARD_SSH_AGENT, False)
    if not isinstance(forward, bool):
        raise ValueError("forward_ssh_agent must be a bool")
    handoff = state.get(KEY_ALLOW_EXTERNAL_HANDOFF, False)
    if not isinstance(handoff, bool):
        raise ValueError("allow_external_handoff must be a bool")
    payload: dict[str, Any] = dict(state)
    atomic_write(
        workflow_policy_path(), json.dumps(payload, indent=2) + "\n", mode=_STATE_FILE_MODE
    )
