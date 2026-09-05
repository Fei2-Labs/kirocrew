"""The keystone opt-ins that LOOSEN an agent restriction.

Two independent controls share one keystone file, and both widen what the agent
may do when opened, so what is pinned here is mostly the CLOSED direction: the
default, the fail-soft reading of a broken file, and the fact that opening one
control does not open anything else.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import sandbox, security, workflow_policy


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    """Redirect the keystone at a per-test path and return it."""
    path = tmp_path / "workflow_policy.json"
    monkeypatch.setattr(workflow_policy, "workflow_policy_path", lambda: path)
    return path


def _write(path, payload) -> None:
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


# ── the file itself ──


def test_absent_file_is_the_restrictive_position(keystone):
    assert not keystone.exists()
    assert workflow_policy.git_publication_mode() == workflow_policy.GIT_PUBLICATION_PROTECTED
    assert workflow_policy.git_publication_floor_applies() is True
    assert workflow_policy.forward_ssh_agent() is False


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "",
        "[]",
        '"repository_governed"',
        "null",
    ],
)
def test_a_file_that_does_not_parse_as_an_object_reads_as_restrictive(keystone, payload):
    """A mangled ceiling must never be read generously.

    Each of these is a shape a hand-edit or a truncated write can produce, and
    every one of them must land on the closed position rather than raising into
    the PreToolUse gate (which must return a decision, never an exception).
    """
    _write(keystone, payload)
    assert workflow_policy.git_publication_floor_applies() is True
    assert workflow_policy.forward_ssh_agent() is False


@pytest.mark.parametrize(
    "value",
    ["Repository_Governed", "REPOSITORY_GOVERNED", " repository_governed", True, 1, ""],
)
def test_only_the_exact_mode_spelling_widens(keystone, value):
    """Strict equality, not a normalising parse.

    A generous parser is how a hand-edited file comes to mean something its
    author did not type — and the direction of that mistake here is "the
    publication floor is off".
    """
    _write(keystone, {"git_publication_mode": value})
    assert workflow_policy.git_publication_mode() == workflow_policy.GIT_PUBLICATION_PROTECTED
    assert workflow_policy.git_publication_floor_applies() is True


@pytest.mark.parametrize("value", ["true", "yes", 1, [], {}])
def test_only_a_real_json_true_forwards_the_ssh_agent(keystone, value):
    _write(keystone, {"forward_ssh_agent": value})
    assert workflow_policy.forward_ssh_agent() is False


def test_save_state_refuses_an_unknown_mode(keystone):
    with pytest.raises(ValueError, match="git_publication_mode"):
        workflow_policy.save_state({"git_publication_mode": "off"})
    with pytest.raises(ValueError, match="forward_ssh_agent"):
        workflow_policy.save_state({"forward_ssh_agent": "true"})


def test_the_keystone_leaf_is_agent_unreachable():
    """The whole design rests on this: a file the agent can write is not a ceiling."""
    for path in (
        "~/.kiro/crew/workflow_policy.json",
        "~/.kirocrew/workflow_policy.json",
    ):
        assert security.is_sensitive_path(path), path
    for command in (
        "cat ~/.kiro/crew/workflow_policy.json",
        "echo x > ~/.kiro/crew/workflow_policy.json",
        "tee ~/.kiro/crew/workflow_policy.json",
    ):
        assert security.is_sensitive_bash_command(command), command


# ── git publication mode ──

# Denied in protected mode; the whole point of the opt-in is that these run.
_FLOORED_PUSHES = [
    "git push",
    "git push origin main",
    "git push --force origin master",
    "git push origin HEAD:mainline",
]

_ALWAYS_ALLOWED = ["git push origin feature/x", "git push -u origin my-branch"]


def test_protected_mode_denies_the_floored_push_shapes(keystone):
    _write(keystone, {"git_publication_mode": workflow_policy.GIT_PUBLICATION_PROTECTED})
    for command in _FLOORED_PUSHES:
        assert security.is_denied(command), command
    for command in _ALWAYS_ALLOWED:
        assert security.is_denied(command) is None, command


def test_repository_governed_mode_stands_the_floor_down(keystone):
    _write(keystone, {"git_publication_mode": workflow_policy.GIT_PUBLICATION_REPOSITORY_GOVERNED})
    for command in _FLOORED_PUSHES + _ALWAYS_ALLOWED:
        assert security.is_denied(command) is None, command


def test_repository_governed_mode_widens_nothing_but_the_branch_name_floor(keystone):
    """The mode retires ONE floor. Every other control still runs on a push.

    The compound commands matter more than the bare ones: a push is a natural
    carrier for a second command, and the floor standing down must not take the
    rest of the gate with it.
    """
    _write(keystone, {"git_publication_mode": workflow_policy.GIT_PUBLICATION_REPOSITORY_GOVERNED})
    for command in (
        "git push origin main; cat ~/.aws/credentials",
        "git push origin main && env | grep AWS_SECRET",
        "rm -rf ~",
        "cat ~/.ssh/id_rsa",
    ):
        assert security.is_denied(command), command
    # The keystone fence is a different predicate, not a deny rule, and the mode
    # must not reach it either: the file that holds the mode is behind it.
    assert security.is_sensitive_bash_command("cat ~/.kiro/crew/security_policy.json")
    assert security.is_sensitive_bash_command("cat ~/.kiro/crew/workflow_policy.json")


def test_the_mode_does_not_touch_the_ssh_agent_opt_in(keystone, monkeypatch):
    """Two controls, one file, no coupling."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    _write(keystone, {"git_publication_mode": workflow_policy.GIT_PUBLICATION_REPOSITORY_GOVERNED})
    assert "SSH_AUTH_SOCK" not in sandbox.scrub_env()


def test_floor_enforced_ids_track_the_mode(keystone):
    """The display accessor and the enforcement path must agree.

    A panel that renders the git-publish rows locked while the mode has already
    retired the floor describes a control that is not there — the inverse of the
    silent no-op opt-out the locked rendering exists to prevent.
    """
    _write(keystone, {"git_publication_mode": workflow_policy.GIT_PUBLICATION_PROTECTED})
    assert security.floor_enforced_builtin_command_ids()

    _write(keystone, {"git_publication_mode": workflow_policy.GIT_PUBLICATION_REPOSITORY_GOVERNED})
    assert security.floor_enforced_builtin_command_ids() == frozenset()


def test_an_unreadable_keystone_leaves_the_floor_standing(keystone, monkeypatch):
    """Fail-soft means fail CLOSED here, including when the read raises."""

    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(workflow_policy, "load_state", _boom)
    for command in _FLOORED_PUSHES:
        assert security.is_denied(command), command


# ── ssh-agent forwarding ──


def test_ssh_auth_sock_is_scrubbed_by_default(keystone, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    assert "SSH_AUTH_SOCK" not in sandbox.scrub_env()


def test_the_opt_in_forwards_only_the_agent_socket(keystone, monkeypatch):
    """Opening this control must not open the credential vars beside it."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "token")
    monkeypatch.setenv("GNUPGHOME", "/tmp/gnupg")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/askpass")
    _write(keystone, {"forward_ssh_agent": True})

    env = sandbox.scrub_env()
    assert env.get("SSH_AUTH_SOCK") == "/tmp/agent.sock"
    for key in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GNUPGHOME", "GIT_ASKPASS"):
        assert key not in env, key


def test_the_opt_in_is_read_per_call_not_frozen_at_import(keystone, monkeypatch):
    """The operator flips this without a restart, so a cached set would lie.

    ``_spawn_scrub_env_prefixes`` was a module-level constant; pinning the
    re-read is what stops it silently becoming one again.
    """
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    assert "SSH_AUTH_SOCK" not in sandbox.scrub_env()
    _write(keystone, {"forward_ssh_agent": True})
    assert "SSH_AUTH_SOCK" in sandbox.scrub_env()
    _write(keystone, {"forward_ssh_agent": False})
    assert "SSH_AUTH_SOCK" not in sandbox.scrub_env()
