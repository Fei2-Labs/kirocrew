"""Disconnect's local half: revoking the grant artifacts kiro-cli stored.

The slice these cover exists because removing the MCP entry alone left a usable
refresh token on disk, so a later reconnect resumed a grant the user believed was
gone. Every assertion here is about that gap or about telling the truth when the
removal only partly succeeds.

Boundary these tests also pin: the artifacts are stat-ed and unlinked, never
opened, so no token material can enter the process.
"""

from __future__ import annotations

import pathlib

import pytest

from kiro_crew.connections import mint

_URL = "https://mcp.notion.com/mcp"


def _write_grant(directory: pathlib.Path, url: str = _URL) -> tuple[pathlib.Path, pathlib.Path]:
    """Lay down the paired artifacts kiro-cli writes for a granted provider."""
    token, registration = mint.grant_artifact_paths(url, cache_dir=directory)
    token.write_text("{}", encoding="utf-8")
    registration.write_text("{}", encoding="utf-8")
    return token, registration


def test_revoke_unlinks_both_paired_artifacts(tmp_path: pathlib.Path) -> None:
    token, registration = _write_grant(tmp_path)
    assert mint.grant_present(_URL, cache_dir=tmp_path) is True

    removed = mint.revoke_local_grant(_URL, cache_dir=tmp_path)

    assert sorted(removed) == ["registration", "token"]
    assert not token.exists()
    assert not registration.exists()
    assert mint.grant_present(_URL, cache_dir=tmp_path) is False


def test_revoke_is_idempotent_on_a_provider_with_no_grant(tmp_path: pathlib.Path) -> None:
    assert mint.revoke_local_grant(_URL, cache_dir=tmp_path) == []
    assert mint.surviving_grant_artifacts(_URL, cache_dir=tmp_path) == []


def test_revoke_leaves_the_aws_sso_single_file_form_alone(tmp_path: pathlib.Path) -> None:
    """The cache directory mixes in SSO's ``{sha256}.json``; it is not ours."""
    _write_grant(tmp_path)
    sso = tmp_path / f"{mint.grant_key(_URL)}.json"
    sso.write_text("{}", encoding="utf-8")

    mint.revoke_local_grant(_URL, cache_dir=tmp_path)

    assert sso.exists(), "revoke deleted an AWS SSO artifact it does not own"


def test_surviving_names_the_artifact_left_behind(tmp_path: pathlib.Path) -> None:
    token, _registration = mint.grant_artifact_paths(_URL, cache_dir=tmp_path)
    token.write_text("{}", encoding="utf-8")

    assert mint.surviving_grant_artifacts(_URL, cache_dir=tmp_path) == ["token"]


def test_a_half_removed_grant_is_reported_not_claimed_done(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honesty regression: an artifact that cannot be unlinked is surfaced.

    Reporting only what came off would let Disconnect delete the token, leave the
    registration behind, and still answer "done" -- which is the state a later
    reconnect resumes from.
    """
    _token, registration = _write_grant(tmp_path)
    real_unlink = pathlib.Path.unlink

    def refuse_registration(self: pathlib.Path, *args: object, **kwargs: object) -> None:
        if self == registration:
            raise OSError("locked by another process")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", refuse_registration)

    removed = mint.revoke_local_grant(_URL, cache_dir=tmp_path)

    assert removed == ["token"], "only the token could be removed"
    assert mint.surviving_grant_artifacts(_URL, cache_dir=tmp_path) == ["registration"]
    assert registration.exists()


def test_revoke_never_opens_an_artifact(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grant lifecycle, not credential access: no read path may be taken."""
    _write_grant(tmp_path)

    def forbid_open(self: pathlib.Path, *args: object, **kwargs: object) -> None:
        raise AssertionError(f"revoke opened {self.name}; it may only stat and unlink")

    monkeypatch.setattr(pathlib.Path, "open", forbid_open)
    monkeypatch.setattr(pathlib.Path, "read_text", forbid_open)
    monkeypatch.setattr(pathlib.Path, "read_bytes", forbid_open)

    assert sorted(mint.revoke_local_grant(_URL, cache_dir=tmp_path)) == [
        "registration",
        "token",
    ]


def test_labels_are_bound_to_the_right_files(tmp_path: pathlib.Path) -> None:
    """A reorder in ``grant_artifact_paths`` must not silently swap the labels."""
    token, registration = mint.grant_artifact_paths(_URL, cache_dir=tmp_path)
    labelled = dict(mint._labelled_grant_artifacts(_URL, cache_dir=tmp_path))

    assert labelled == {"token": token, "registration": registration}
    assert labelled["token"].name.endswith(".token.json")
    assert labelled["registration"].name.endswith(".registration.json")


# ── HTTP surface ──
#
# The handler imports its collaborators function-locally (the module's own
# boot-path convention), so patching them on their SOURCE modules is what takes
# effect at call time.

import contextlib  # noqa: E402
import types  # noqa: E402

import pytest_asyncio  # noqa: E402  (imported for its marker plugin)
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from kiro_crew import agent as agent_mod  # noqa: E402
from kiro_crew import mcp_discovery  # noqa: E402
from kiro_crew.connections import get_provider  # noqa: E402
from kiro_crew.dashboard.handlers import connections  # noqa: E402
from kiro_crew.dashboard.handlers import mcp as mcp_handlers  # noqa: E402

_ = pytest_asyncio

_SLUG = "notion"


def _provider_url() -> str:
    provider = get_provider(_SLUG)
    assert provider is not None, "registry no longer ships the fixture provider"
    return str(provider["mcp_url"])


@contextlib.asynccontextmanager
async def _no_lock():
    yield


def _entry(name: str, url: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, url=url)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    removed: list[str],
    surviving: list[str],
    inventory: list[types.SimpleNamespace],
    purged: list[str],
    revoked: list[str] | None = None,
    audits: list[dict] | None = None,
    raw_specs: dict | None = None,
) -> None:
    """Neutralize every side effect except the decisions under test.

    ``_offload_config_write`` is deliberately left REAL: it is the shielded wrapper
    the purge must go through, so letting it drive the fake purge exercises that
    path rather than stubbing it away. ``raw_specs`` is the per-scope spec dict the
    sharing sweep reads (disabled entries included); it defaults to mirroring the
    probe inventory so single-view tests stay unchanged.
    """
    seen_revokes = revoked if revoked is not None else []

    def _revoke(url: str) -> list[str]:
        seen_revokes.append(url)
        return list(removed)

    def _audit(**kwargs: object) -> None:
        if audits is not None:
            audits.append(dict(kwargs))

    monkeypatch.setattr(mint, "revoke_local_grant", _revoke)
    monkeypatch.setattr(mint, "surviving_grant_artifacts", lambda _url: list(surviving))

    async def _no_mint(_slug: str, _token: object) -> bool:
        return False

    monkeypatch.setattr(mint, "cancel_mint", _no_mint)
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: list(inventory))
    default_raw = {"kirocrew": {s.name: {"url": s.url} for s in inventory}}
    monkeypatch.setattr(
        mcp_discovery,
        "_load_mcp_json_by_source",
        lambda: raw_specs if raw_specs is not None else default_raw,
    )
    monkeypatch.setattr(mcp_handlers, "_get_mcp_lock", _no_lock)
    monkeypatch.setattr(
        mcp_handlers, "_purge_server_config", lambda name: purged.append(name) or {}
    )
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", lambda: None)
    monkeypatch.setattr(connections, "sel", lambda: types.SimpleNamespace(log_api_access=_audit))


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_post("/api/connections/disconnect", connections.api_connections_disconnect)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _disconnect() -> dict:
    client = await _client()
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": _SLUG})
        assert resp.status == 200
        return await resp.json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_disconnect_revokes_the_grant_and_removes_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    purged: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=purged,
    )

    body = await _disconnect()

    assert body == {
        "ok": True,
        "disconnected": _SLUG,
        "grantRemoved": True,
        "grantSurviving": [],
        "entryRemoved": True,
        "grantSharedWith": [],
    }
    assert purged == [_SLUG]


@pytest.mark.asyncio
async def test_disconnect_keeps_a_grant_another_entry_still_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The data-loss guard: a grant is keyed by ENDPOINT, not by entry.

    ``grant_key`` is a sha256 over the URL alone, so one artifact pair serves every
    entry talking to that endpoint. Revoking because *our* entry went would
    deauthorize a user's own separately-named server at the same URL, and its refresh
    token is not recoverable locally.
    """
    purged: list[str] = []
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=["token", "registration"],
        inventory=[
            _entry(_SLUG, _provider_url()),
            _entry("notion-work", _provider_url()),
        ],
        purged=purged,
        revoked=revoked,
    )

    body = await _disconnect()

    assert revoked == [], "disconnect revoked a grant another entry still uses"
    assert body["grantRemoved"] is False
    assert body["grantSharedWith"] == ["notion-work"]
    # Our own entry still comes out -- only the shared credential is spared.
    assert body["entryRemoved"] is True
    assert purged == [_SLUG]


@pytest.mark.asyncio
async def test_a_shared_endpoint_is_not_reported_as_a_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifacts surviving BY DESIGN are not the same event as a failed unlink."""
    audits: list[dict] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=["token", "registration"],
        inventory=[
            _entry(_SLUG, _provider_url()),
            _entry("notion-work", _provider_url()),
        ],
        purged=[],
        revoked=[],
        audits=audits,
    )

    await _disconnect()

    assert audits, "the disconnect wrote no audit event"
    assert audits[-1]["outcome"] == "ok", "a deliberately kept grant audited as partial"


@pytest.mark.asyncio
async def test_a_query_string_variant_shares_the_grant_and_blocks_the_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grant identity is ``grant_key``, which DROPS the query string.

    ``notion-work → <registry url>?workspace=team`` is a different endpoint by
    ``normalized_endpoint`` but names the SAME artifact pair, so revoking would
    delete the grant it authenticates with. The endpoint comparator would miss it.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=["token", "registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {"notion-work": {"url": _provider_url() + "?workspace=team"}},
        },
    )

    body = await _disconnect()

    assert revoked == [], "revoked a grant a query-variant entry still uses"
    assert body["grantSharedWith"] == ["notion-work"]


@pytest.mark.asyncio
async def test_a_trailing_slash_variant_owns_a_different_grant_and_never_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inverse direction: same endpoint by comparator, DIFFERENT artifact pair.

    ``grant_key`` keeps the path verbatim, so ``/mcp/`` hashes differently from
    ``/mcp``. Treating it as a sharer would skip the revoke for a grant nobody
    else holds and report the survivor as a deliberate keep -- the silent-resume
    regression this endpoint exists to close, rendered as success.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {"notion-slash": {"url": _provider_url() + "/"}},
        },
    )

    body = await _disconnect()

    assert revoked == [_provider_url()], "a non-sharing variant blocked the revoke"
    assert body["grantSharedWith"] == []
    assert body["grantRemoved"] is True


@pytest.mark.asyncio
async def test_a_disabled_entry_still_counts_as_a_grant_sharer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switched-off server still owns its grant.

    ``list_servers`` drops disabled entries outside the KiroCrew scope, so the
    sharing sweep must read the raw specs -- otherwise disabling an entry makes
    its grant deletable, and re-enabling it demands a fresh consent with nothing
    having said so.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=[],
        surviving=["token", "registration"],
        # The probe view sees only our entry; the disabled sharer is invisible to it.
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {"notion-work": {"url": _provider_url(), "disabled": True}},
        },
    )

    body = await _disconnect()

    assert revoked == [], "revoked a grant a disabled entry still owns"
    assert body["grantSharedWith"] == ["notion-work"]


@pytest.mark.asyncio
async def test_a_malformed_config_url_neither_crashes_nor_blocks_the_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One junk entry must not 500 every Disconnect or disable revocation.

    ``grant_key`` raises ``ValueError`` on an unparseable port, and scope files are
    hand-edited. An unparseable URL also cannot NAME our artifact pair -- the pair
    is a hash of a successful parse, and kiro-cli's own parser rejects the same
    shapes -- so it is skipped, not counted as a sharer: counting it would let one
    junk line permanently disable the trust fix under a false "shared" message.
    """
    revoked: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=[],
        revoked=revoked,
        raw_specs={
            "kirocrew": {_SLUG: {"url": _provider_url()}},
            "global": {
                "template-junk": {"url": "http://localhost:${PORT}/mcp"},
                "port-overflow": {"url": "https://host:99999/mcp"},
                "bad-bracket": {"url": "https://[::1/mcp"},
                # The screen parses a STRIPPED value; the hash must hash that same
                # string. urlsplit lstrips only, so a trailing space after an
                # explicit port passes the screen and raises in a raw-string hash.
                "trailing-space": {"url": "https://host:8080 "},
            },
        },
    )

    body = await _disconnect()

    assert body["ok"] is True, "a malformed sibling entry 500'd the disconnect"
    assert body["grantSharedWith"] == []
    assert revoked == [_provider_url()], "junk entries blocked the revoke"


@pytest.mark.asyncio
async def test_disconnect_reports_a_survivor_instead_of_claiming_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-revoked grant is named, not rounded up to "done"."""
    purged: list[str] = []
    _wire(
        monkeypatch,
        removed=["token"],
        surviving=["registration"],
        inventory=[_entry(_SLUG, _provider_url())],
        purged=purged,
    )

    body = await _disconnect()

    assert body["grantRemoved"] is True
    assert body["grantSurviving"] == ["registration"]
    # The entry still comes out: a half-revoked grant is reason enough to stop
    # advertising the server, and the response says which half held.
    assert body["entryRemoved"] is True
    assert purged == [_SLUG]


@pytest.mark.asyncio
async def test_disconnect_leaves_a_same_named_server_on_another_endpoint_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity is the endpoint, never the entry name.

    A user-added server that merely happens to be called ``notion`` is not the
    Notion card's connection. Purging on the name alone would delete their server
    because they clicked Disconnect on ours.
    """
    purged: list[str] = []
    _wire(
        monkeypatch,
        removed=["token", "registration"],
        surviving=[],
        inventory=[_entry(_SLUG, "https://notion.example.internal/mcp")],
        purged=purged,
    )

    body = await _disconnect()

    assert body["entryRemoved"] is False
    assert purged == [], "disconnect deleted a server it does not own"
    # The grant is keyed on the REGISTRY url, so it is still ours to remove.
    assert body["grantRemoved"] is True


@pytest.mark.asyncio
async def test_disconnect_rejects_a_provider_outside_the_registry() -> None:
    client = await _client()
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": "not-a-provider"})
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()
    assert body["code"] == "unknown_provider"
