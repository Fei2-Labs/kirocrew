"""Admission-boundary tests for coordinator-authoritative HTTP commands."""

from __future__ import annotations

import hashlib
import json

import pytest

from kiro_crew.dashboard.handlers.messaging import _validated_command_identity


def _identified(operation: str, body: dict[str, object]) -> dict[str, object]:
    payload_json = json.dumps(
        {"operation": operation, **body}, separators=(",", ":"), sort_keys=True
    )
    return {
        **body,
        "command_id": "a" * 32,
        "idempotency_key": "b" * 32,
        "payload_hash": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
    }


def test_old_caller_without_identity_stays_on_compatibility_path() -> None:
    assert _validated_command_identity({"task": "legacy"}, "spawn", require_run_id=True) is None


def test_command_identity_recomputes_canonical_payload_hash() -> None:
    body = _identified("spawn", {"task": "work", "run_id": "c" * 8})

    identity = _validated_command_identity(body, "spawn", require_run_id=True)

    assert identity is not None
    assert identity[:3] == ("a" * 32, "b" * 32, body["payload_hash"])
    assert identity[3] == '{"operation":"spawn","run_id":"cccccccc","task":"work"}'


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"run_id": "c" * 8, "command_id": "a" * 32}, "incomplete_command_identity"),
        (
            {
                "run_id": "not-hex!",
                "command_id": "a" * 32,
                "idempotency_key": "b" * 32,
                "payload_hash": "c" * 64,
            },
            "invalid_run_id",
        ),
    ],
)
def test_invalid_command_identity_fails_closed(body: dict[str, object], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        _validated_command_identity(body, "spawn", require_run_id=True)


def test_changed_payload_is_rejected_even_with_claimed_hash() -> None:
    body = _identified("steer", {"message": "first", "mode": "interrupt"})
    body["message"] = "changed"

    with pytest.raises(ValueError, match="payload_hash_mismatch"):
        _validated_command_identity(body, "steer", require_run_id=False)
