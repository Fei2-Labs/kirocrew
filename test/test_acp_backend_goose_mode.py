"""goose routing is seeded on the process environment and verified at startup."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.acp import tool_gate
from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.types import ACP_BACKEND_GOOSE, ACP_BACKEND_PI


class TestGooseRoutingProbe:
    def test_doctor_reason_names_the_environment_seed(self, tmp_path: Path) -> None:
        verdict, reason = tool_gate.routing_verdict(ACP_BACKEND_GOOSE, tmp_path)
        assert verdict is Verdict.ROUTED
        assert "GOOSE_MODE=approve" in reason
        assert "auto" in reason

    def test_pi_reason_stays_structural(self, tmp_path: Path) -> None:
        verdict, reason = tool_gate.routing_verdict(ACP_BACKEND_PI, tmp_path)
        assert verdict is Verdict.ROUTED
        assert "session/request_permission" in reason or "asks per privileged tool" in reason


class TestGooseRoutingVerification:
    def _client(self, backend: str = ACP_BACKEND_GOOSE) -> AcpClient:
        return AcpClient(acp_backend=backend)

    def test_accepts_resolved_approve_mode(self) -> None:
        self._client()._verify_goose_routing(
            {"modes": {"currentModeId": "approve", "availableModes": [{"id": "approve"}]}}
        )

    def test_refuses_resolved_auto_mode(self) -> None:
        with pytest.raises(AcpToolGateUnroutable, match="auto"):
            self._client()._verify_goose_routing(
                {"modes": {"currentModeId": "auto", "availableModes": [{"id": "auto"}]}}
            )

    def test_refuses_when_session_mode_is_missing(self) -> None:
        with pytest.raises(AcpToolGateUnroutable):
            self._client()._verify_goose_routing({})

    def test_other_harness_does_not_use_goose_check(self) -> None:
        self._client(ACP_BACKEND_PI)._verify_goose_routing({})
