"""Tests for kiro_crew.dashboard.handlers.secrets."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers.secrets import setup_secrets_routes
from kiro_crew.secrets import SecretVault


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    """Create a vault with test data."""
    vault = SecretVault(tmp_path)
    vault._set_sync("TEST_KEY", "test-value-123")
    vault._set_sync("DB_PASS", "hunter2")
    return tmp_path


@pytest.fixture()
def empty_vault_dir(tmp_path: Path) -> Path:
    return tmp_path


class TestApiSecretsList:
    """Tests for GET /api/secrets."""

    @pytest.mark.asyncio
    async def test_lists_names_sorted(self, vault_dir: Path) -> None:
        app = web.Application()
        setup_secrets_routes(app)

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                data = await resp.json()
                assert data == {"names": ["DB_PASS", "TEST_KEY"]}

    @pytest.mark.asyncio
    async def test_empty_vault(self, empty_vault_dir: Path) -> None:
        app = web.Application()
        setup_secrets_routes(app)

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(empty_vault_dir)
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/secrets")
                assert resp.status == 200
                data = await resp.json()
                assert data == {"names": []}


class TestApiSecretsSet:
    """Tests for POST /api/secrets."""

    @pytest.mark.asyncio
    async def test_stores_secret(self, tmp_path: Path) -> None:
        app = web.Application()
        setup_secrets_routes(app)

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/secrets",
                    json={"name": "NEW_KEY", "value": "new-value"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True

                # Verify stored
                vault = SecretVault(tmp_path)
                assert vault.get("NEW_KEY").reveal() == "new-value"

    @pytest.mark.asyncio
    async def test_missing_name(self, tmp_path: Path) -> None:
        app = web.Application()
        setup_secrets_routes(app)

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"value": "x"})
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_value(self, tmp_path: Path) -> None:
        app = web.Application()
        setup_secrets_routes(app)

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(tmp_path)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "X"})
                assert resp.status == 400


class TestApiSecretsDelete:
    """Tests for DELETE /api/secrets/{name}."""

    @pytest.mark.asyncio
    async def test_deletes_secret(self, vault_dir: Path) -> None:
        app = web.Application()
        setup_secrets_routes(app)

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/TEST_KEY")
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True

                vault = SecretVault(vault_dir)
                assert vault.get("TEST_KEY") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, vault_dir: Path) -> None:
        app = web.Application()
        setup_secrets_routes(app)

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.secrets.config_dir", return_value=str(vault_dir)):
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/secrets/MISSING")
                assert resp.status == 200  # delete is idempotent


class TestApiSecretsSetInputValidation:
    """POST /api/secrets rejects well-formed JSON of the wrong shape with 400.

    These bodies all parse as valid JSON, so they get past the JSONDecodeError
    guard. Before the type checks, `body.get("name", "").strip()` raised
    AttributeError on each of them and surfaced as an HTTP 500.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ([{"name": "A", "value": "b"}], "invalid_body"),  # JSON array
            ("just a string", "invalid_body"),  # JSON string
            (42, "invalid_body"),  # JSON number
            ({"name": 123, "value": "b"}, "invalid_name_type"),  # non-string name
            ({"name": ["A"], "value": "b"}, "invalid_name_type"),  # list name
            ({"name": None, "value": "b"}, "invalid_name_type"),  # null name
            ({"value": "b"}, "invalid_name_type"),  # name absent entirely
            ({"name": "A", "value": 123}, "invalid_value_type"),  # non-string value
            ({"name": "A", "value": {"k": "v"}}, "invalid_value_type"),  # dict value
            ({"name": "A"}, "invalid_value_type"),  # value absent entirely
        ],
    )
    async def test_rejects_wrong_types_with_400(
        self, empty_vault_dir: Path, body: object, code: str
    ) -> None:
        app = web.Application()
        setup_secrets_routes(app)

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json=body)
                assert resp.status == 400
                data = await resp.json()
                assert data["code"] == code
                # Nothing was written to the vault on a rejected request.
                assert SecretVault(empty_vault_dir).list_names() == []

    @pytest.mark.asyncio
    async def test_accepts_valid_string_payload(self, empty_vault_dir: Path) -> None:
        """The happy path still works after the added type checks."""
        app = web.Application()
        setup_secrets_routes(app)

        from aiohttp.test_utils import TestClient, TestServer

        with patch(
            "kiro_crew.dashboard.handlers.secrets.config_dir",
            return_value=str(empty_vault_dir),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/secrets", json={"name": "  PADDED  ", "value": "v"})
                assert resp.status == 200
                data = await resp.json()
                # Name is still trimmed, as before.
                assert data["name"] == "PADDED"
                assert SecretVault(empty_vault_dir).list_names() == ["PADDED"]
