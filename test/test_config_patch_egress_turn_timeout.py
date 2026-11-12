"""PATCH /api/config/kirocrew: telemetry egress gating and the chat turn timeout.

Split out of ``test_config_patch.py`` for the file-line cap; the app builders,
patch helper and fixtures are imported from it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_config_patch import (  # noqa: F401
    _make_app,
    _owner_caller,
    _patch,
    _windows_exec_close_ack,
    tmp_config,
)



class TestTelemetryEnabledEgressGate:
    """The switch promises local-only, so it must not reach a state that exports.

    `_build_recorder` attaches an OTLP reader per provider destination, so where egress
    is configured (`telemetry.otlp_endpoint` or an edition collector) enabling is
    refused; disabling always works.
    """

    def _seed_endpoint(self, cfg_path, endpoint: str) -> None:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        data.setdefault("telemetry", {})["otlp_endpoint"] = endpoint
        cfg_path.write_text(json.dumps(data), encoding="utf-8")

    @pytest.mark.asyncio
    async def test_enable_is_refused_when_an_edition_supplies_a_destination(
        self, tmp_config
    ) -> None:
        """Egress posture is NOT the config key. An edition that supplies its own
        collector must refuse the same enable, or the local-only promise would hold
        only for the default provider and the panel would report "nothing is
        exported" while metrics left the machine."""
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context
        from kiro_crew.platform.context import reset_context, set_context
        from kiro_crew.platform.interfaces import OtlpDestination

        class _EditionTelemetry:
            def record_event(self, event_type, data):
                return None

            def frontend_rum_config(self):
                return None

            def otlp_destinations(self, cfg):
                return (
                    OtlpDestination(
                        "edition-collector",
                        "https://collector.internal:4318/v1/metrics",
                        frozenset({"metrics"}),
                    ),
                )

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, telemetry=_EditionTelemetry()))
        try:
            # telemetry.otlp_endpoint stays EMPTY on disk — the old guard read that
            # key and would have allowed this write.
            async with TestClient(TestServer(_make_app())) as c:
                resp = await _patch(c, "telemetry.enabled", True)
                assert resp.status == 409
            data = json.loads(tmp_config.read_text(encoding="utf-8"))
            assert data.get("telemetry", {}).get("enabled") is not True
        finally:
            reset_context()

    @pytest.mark.asyncio
    async def test_enable_is_refused_when_an_endpoint_is_configured(self, tmp_config) -> None:
        self._seed_endpoint(tmp_config, "http://otel.internal:4318/v1/metrics")
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "telemetry.enabled", True)
            assert resp.status == 409
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["telemetry"].get("enabled") is not True

    @pytest.mark.asyncio
    async def test_enable_is_refused_when_the_egress_posture_cannot_be_resolved(
        self, tmp_config
    ) -> None:
        """A provider that raises must not read as "no egress". Permitting the
        toggle there would let the recovered provider attach an OTLP reader on the
        next build - egress the operator was told would not happen."""
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context
        from kiro_crew.platform.context import reset_context, set_context

        class _BrokenTelemetry:
            def record_event(self, event_type, data):
                return None

            def frontend_rum_config(self):
                return None

            def otlp_destinations(self, cfg):
                raise RuntimeError("collector discovery failed")

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, telemetry=_BrokenTelemetry()))
        try:
            async with TestClient(TestServer(_make_app())) as c:
                assert (await _patch(c, "telemetry.enabled", True)).status == 409
            data = json.loads(tmp_config.read_text(encoding="utf-8"))
            assert data.get("telemetry", {}).get("enabled") is not True
        finally:
            reset_context()

    @pytest.mark.asyncio
    async def test_disable_is_still_allowed_when_an_endpoint_is_configured(
        self, tmp_config
    ) -> None:
        # Tightening always composes — refusing it would strand a user who wants
        # collection off on exactly the host where it also exports.
        self._seed_endpoint(tmp_config, "http://otel.internal:4318/v1/metrics")
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "telemetry.enabled", False)).status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["telemetry"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_blank_endpoint_does_not_block_enabling(self, tmp_config) -> None:
        self._seed_endpoint(tmp_config, "   ")
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "telemetry.enabled", True)).status == 200

    @pytest.mark.asyncio
    async def test_refused_enable_does_not_touch_the_recorder(self, tmp_config) -> None:
        self._seed_endpoint(tmp_config, "http://otel.internal:4318/v1/metrics")
        with patch("kiro_crew.metrics.provider.shutdown") as reset:
            async with TestClient(TestServer(_make_app())) as c:
                assert (await _patch(c, "telemetry.enabled", True)).status == 409
        reset.assert_not_called()


class TestChatTurnTimeout:
    """Settings → Chat → Advanced → "Turn Time Limit (seconds)".

    The write gate uses the loader's own bounds, so Settings can never save a
    ceiling the loader would then rewrite, and a saved value loads as written.
    """

    @pytest.mark.asyncio
    async def test_a_value_inside_the_range_round_trips(self, tmp_config) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "agent.chat_turn_timeout_secs", 28800)
            assert resp.status == 200, await resp.text()
        stored = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert stored["agent"]["chat_turn_timeout_secs"] == 28800
        assert KiroCrewConfig.load().agent.chat_turn_timeout_secs == 28800

    @pytest.mark.asyncio
    async def test_both_loader_bounds_are_writable(self, tmp_config) -> None:
        from kiro_crew.config.sections import CHAT_TURN_TIMEOUT_MAX, CHAT_TURN_TIMEOUT_MIN

        async with TestClient(TestServer(_make_app())) as c:
            for value in (CHAT_TURN_TIMEOUT_MIN, CHAT_TURN_TIMEOUT_MAX):
                resp = await _patch(c, "agent.chat_turn_timeout_secs", value)
                assert resp.status == 200, await resp.text()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [299, 86401, 0, -1, "four hours"])
    async def test_a_value_the_loader_would_clamp_is_refused(self, tmp_config, value) -> None:
        before = tmp_config.read_text(encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "agent.chat_turn_timeout_secs", value)).status == 400
        assert tmp_config.read_text(encoding="utf-8") == before

    @pytest.mark.asyncio
    async def test_a_saved_two_hour_limit_survives_superseded_default_adoption(
        self, tmp_config, monkeypatch
    ) -> None:
        """7200 is the row's superseded default, and the row auto-adopts.

        Saving it in Settings is an explicit choice, so the next load must not
        read it as a materialized default and delete it.
        """
        from kiro_crew.config import loader as L
        from kiro_crew.config import superseded_defaults as SD

        monkeypatch.setattr(L, "config_dir", lambda: tmp_config.parent)
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "agent.chat_turn_timeout_secs", 7200)
            assert resp.status == 200, await resp.text()
        assert SD.acked_superseded() == {
            "agent.chat_turn_timeout_secs": 7200,
            **_windows_exec_close_ack(L),
        }
        assert L.KiroCrewConfig.load().agent.chat_turn_timeout_secs == 7200
        stored = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert stored["agent"]["chat_turn_timeout_secs"] == 7200

    @pytest.mark.asyncio
    async def test_another_value_records_no_ack(self, tmp_config, monkeypatch) -> None:
        from kiro_crew.config import loader as L
        from kiro_crew.config import superseded_defaults as SD

        monkeypatch.setattr(L, "config_dir", lambda: tmp_config.parent)
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "agent.chat_turn_timeout_secs", 10800)).status == 200
        assert SD.acked_superseded() == _windows_exec_close_ack(L)

    @pytest.mark.asyncio
    async def test_a_refused_ack_aborts_the_write(self, tmp_config, monkeypatch) -> None:
        """Without its ack the saved 7200 would be deleted on the next load."""
        from kiro_crew.config import superseded_defaults as SD

        def _refuse(_mutate):
            raise SD.AckPathRefused("refused")

        monkeypatch.setattr(SD, "_update_acked", _refuse)
        before = tmp_config.read_text(encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "agent.chat_turn_timeout_secs", 7200)).status == 500
        assert tmp_config.read_text(encoding="utf-8") == before
