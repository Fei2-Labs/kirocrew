"""Conductor agent installer + bundled acceptance evaluator.

The installer test mirrors the research-agent installer test's shape: stub the
agents dir and ``build_agent_config``, run the installer, assert on the JSON it
wrote. The evaluator tests run the real script over stdin/stdout — it is the
deterministic half of the conductor's patrol, so its verdict vocabulary is
pinned here.

The ``cmd`` fixtures deliberately use ``git`` rather than ``sys.executable``:
bare interpreters are NOT on the evaluator's allowlist (a spec could otherwise
name ``python -c <payload>``), and pinning that is one of the tests below.
"""

import json
import subprocess
import sys
from pathlib import Path

from kiro_crew import agent
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

SKILL_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "builtin_skills" / "goal-conductor"
)
SCRIPT = SKILL_DIR / "scripts" / "accept_eval.py"

#: An allowlisted command that always exits 0 — the "pass" fixture.
_OK_ARGV = ["git", "--version"]
#: An allowlisted command that always exits non-zero — the "fail" fixture.
_FAIL_ARGV = ["git", "rev-parse", "--verify", "refs/heads/kirocrew-no-such-ref"]


class TestConductorInstaller:
    def _install(self, tmp_path, monkeypatch, *, may_auto_approve=None):
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr(
            agent,
            "build_agent_config",
            lambda: {
                "name": "kirocrew",
                "prompt": "file://x",
                "mcpServers": {
                    "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]},
                    "builder-mcp": {"command": "/x/builder", "args": []},
                },
                "tools": ["fs_write", "@kirocrew-core"],
                "allowedTools": ["@kirocrew-core"],
            },
        )
        monkeypatch.setattr(
            agent,
            "_kirocrew_mcp_invocation",
            lambda sub: ("/resolved/kirocrew", [sub]),
        )
        # Pin the ceiling predicate: the default is an ungoverned host (keep every
        # grant), and the governed case gets its own test below.
        monkeypatch.setattr(agent, "_may_auto_approve", may_auto_approve or (lambda ref: True))
        agent._install_conductor_agent()
        return json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))

    def test_identity_and_charter(self, tmp_path, monkeypatch):
        data = self._install(tmp_path, monkeypatch)
        assert data["name"] == "kirocrew-conductor"
        assert "work item" in data["prompt"]

    def test_no_write_tool_and_dashboard_not_preapproved(self, tmp_path, monkeypatch):
        """The two deliberate security properties of the spec.

        No ``fs_write``: the conductor cannot do a work item's work itself.
        ``@kirocrew-dashboard`` and ``execute_bash`` reachable but NOT in
        ``allowedTools``: their calls must keep passing through the tool-call
        hook where the deny floor and governance ceiling apply.
        """
        data = self._install(tmp_path, monkeypatch)
        assert "fs_write" not in data["tools"]
        assert "@kirocrew-dashboard" in data["tools"]
        assert "@kirocrew-dashboard" not in data["allowedTools"]
        assert "execute_bash" in data["tools"]
        assert "execute_bash" not in data["allowedTools"]

    def test_mcp_surface_is_narrowed_to_core_plus_dashboard(self, tmp_path, monkeypatch):
        """Inherited servers the conductor has no charter for are dropped."""
        data = self._install(tmp_path, monkeypatch)
        assert set(data["mcpServers"]) == {"kirocrew-core", "kirocrew-dashboard"}
        assert data["mcpServers"]["kirocrew-dashboard"]["args"] == ["mcp-dashboard"]

    def test_dashboard_entry_omits_managed_metadata_on_a_default_install(
        self, tmp_path, monkeypatch
    ):
        """Neither helper contributes by default, so the emitted entry stays minimal.

        Pinned explicitly rather than read off the ambient environment: the
        repo-root conftest pins ``KIROCREW_HOME`` for every test, so the real
        ``_managed_mcp_env()`` legitimately returns a pin in-suite.
        """
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: False)
        monkeypatch.setattr(agent, "_managed_mcp_env", dict)
        dash = self._install(tmp_path, monkeypatch)["mcpServers"]["kirocrew-dashboard"]
        assert dash == {"command": "/resolved/kirocrew", "args": ["mcp-dashboard"]}

    def test_dashboard_entry_carries_managed_server_metadata(self, tmp_path, monkeypatch):
        """The hand-built dashboard entry is enriched like every managed server.

        Without ``"type": "registry"`` a registry-mode client silently drops the
        entry, so the conductor's session-control tools never launch. Without the
        ``KIROCREW_HOME`` pin the shim reads the default data home while the
        gateway runs under an override, so session control acts on a different
        session store than it reports on.
        """
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: True)
        monkeypatch.setattr(agent, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/tmp/override"})
        data = self._install(tmp_path, monkeypatch)
        dash = data["mcpServers"]["kirocrew-dashboard"]
        assert dash["type"] == "registry"
        assert dash["env"] == {"KIROCREW_HOME": "/tmp/override"}

    def test_grants_pass_through_the_governance_ceiling(self, tmp_path, monkeypatch):
        """``allowedTools`` never reaches the PreToolUse gate, so it is filtered.

        A ceiling with an opinion about ``@kirocrew-core`` must not be silently
        bypassed by a static grant list: the ref stays MOUNTED (still in
        ``tools``) but loses its blanket auto-approve, so its calls prompt and
        the gate applies the real per-tool rule.
        """
        data = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core",
        )
        assert "@kirocrew-core" in data["tools"]
        assert "@kirocrew-core" not in data["allowedTools"]
        assert data["allowedTools"] == ["session", "report"]

    def test_kas_permissions_are_derived_from_the_filtered_grants(self, tmp_path, monkeypatch):
        """The KAS block is derived, not restated — so the ceiling reaches it too.

        Ungoverned: the ``@kirocrew-core`` grant projects to an ``mcp`` allow
        rule. Governed: the grant is gone, so the rule is too — and the key is
        still present, because its mere presence is what makes KAS load the spec.
        """
        data = self._install(tmp_path, monkeypatch)
        assert data["permissions"] == {
            "rules": [{"capability": "mcp", "match": ["kirocrew-core/*"], "effect": "allow"}]
        }

        governed = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core",
        )
        assert governed["permissions"] == {"rules": []}

    def test_spec_is_registered_as_kirocrew_owned(self):
        """Every managed spec registers in ``OWNED_KIRO_AGENT_FILES``.

        Three consumers key off that tuple (the Playwright convergence sweep,
        ``doctor``'s dead-path repair, connection minting). Absent from it, a
        conductor spec whose resolved MCP command path dies is classified as a
        foreign file and reported as unfixable instead of being repaired.
        """
        assert CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_builtin_skill_does_not_collide_with_the_delegation_skill(self):
        """The packaged skill must NOT be named ``conductor``.

        ``conductor_skill.generate_conductor_skill`` owns
        ``<skills>/conductor/SKILL.md``, and two paths DELETE that file when
        ``agent.conductor_skill`` is false (the default): ``cli_setup`` on every
        setup run and the dashboard config handler on toggle-off. A packaged
        skill sharing the name would be erased on a stock install.
        """
        assert SKILL_DIR.is_dir()
        assert not (_BUILTIN_SKILLS_DIR / "conductor").exists()
        assert "name: goal-conductor" in (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


class TestAcceptEvaluator:
    def _run(self, items):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps({"items": items}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return {r["id"]: r for r in json.loads(proc.stdout)["results"]}

    def test_verdict_vocabulary_across_kinds(self, tmp_path):
        exists = tmp_path / "made"
        exists.write_text("x")
        out = self._run(
            [
                {"id": "ok", "accept": {"kind": "cmd", "argv": _OK_ARGV}},
                {
                    "id": "bad",
                    "accept": {"kind": "cmd", "argv": _FAIL_ARGV, "cwd": str(tmp_path)},
                },
                {"id": "blocked", "accept": {"kind": "cmd", "argv": ["curl", "http://x"]}},
                {"id": "have", "accept": {"kind": "file", "path": str(exists), "exists": True}},
                {
                    "id": "miss",
                    "accept": {"kind": "file", "path": str(tmp_path / "no"), "exists": True},
                },
                {"id": "human", "accept": {"kind": "human_approval"}},
                {"id": "junk", "accept": {"kind": "wat"}},
            ]
        )
        assert out["ok"]["verdict"] == "pass"
        assert out["bad"]["verdict"] == "fail"
        # A command outside the allowlist is refused and surfaced, never
        # silently retried around.
        assert out["blocked"]["verdict"] == "refused"
        assert out["have"]["verdict"] == "pass"
        assert out["miss"]["verdict"] == "fail"
        assert out["human"]["verdict"] == "pending"
        assert out["junk"]["verdict"] == "error"

    def test_bare_interpreters_are_refused(self, tmp_path):
        """The allowlist holds verdict carriers, not arbitrary-code runners.

        An acceptance spec is model-authored. With ``python``/``node``/``npx`` on
        the list, ``python -c <payload>`` would make the list decorative — every
        refusal below is a spec the conductor must surface, not route around.
        """
        out = self._run(
            [
                {
                    "id": "py",
                    "accept": {"kind": "cmd", "argv": [sys.executable, "-c", "print(1)"]},
                },
                {"id": "py3", "accept": {"kind": "cmd", "argv": ["python3", "-c", "print(1)"]}},
                {"id": "node", "accept": {"kind": "cmd", "argv": ["node", "-e", "1"]}},
                {"id": "npx", "accept": {"kind": "cmd", "argv": ["npx", "whatever"]}},
            ]
        )
        assert [r["verdict"] for r in out.values()] == ["refused"] * 4

    def test_one_bad_spec_does_not_hide_the_others(self):
        out = self._run(
            [
                {"id": "broken", "accept": {"kind": "cmd", "argv": []}},
                {"id": "fine", "accept": {"kind": "cmd", "argv": _OK_ARGV}},
            ]
        )
        assert out["broken"]["verdict"] == "error"
        assert out["fine"]["verdict"] == "pass"

    def test_a_non_object_item_does_not_abort_the_run(self):
        """ID extraction is inside the per-item guard.

        ``{"items": [1, {...}]}`` raises on ``.get`` before the handler runs; if
        that raise escaped, the whole evaluation would exit non-zero and every
        sibling verdict would be lost. The malformed entry gets a positional id
        and an ``error`` verdict instead.
        """
        out = self._run([1, "nope", {"id": "fine", "accept": {"kind": "cmd", "argv": _OK_ARGV}}])
        assert out["fine"]["verdict"] == "pass"
        assert out["#0"]["verdict"] == "error"
        assert out["#1"]["verdict"] == "error"
        assert "JSON object" in out["#0"]["evidence"]

    def test_malformed_stdin_is_a_clean_exit_2(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert proc.returncode == 2
