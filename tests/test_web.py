"""Relay of the web interface: administrators only, sandboxed, token in the path."""

from __future__ import annotations

from http import HTTPStatus
import json

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from yarl import URL

from custom_components.hottoh import web

from .conftest import FakeBridge

SANDBOX = f"sandbox {web.SANDBOX_FLAGS}"
# what a sandboxed page sends: its origin is the string "null"
FROM_PAGE = {"Origin": "null"}


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _session(
    client, entry: MockConfigEntry, token: str | None = None
) -> dict[str, str]:
    body = {"entry_id": entry.entry_id}
    if token is not None:
        body["token"] = token
    resp = await client.post("/api/hottoh/session", json=body)
    assert resp.status == HTTPStatus.OK
    return await resp.json()


def _assert_sandboxed(resp) -> None:
    """Headers every answer of the relay carries, errors included."""
    assert resp.headers["Content-Security-Policy"] == SANDBOX
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Access-Control-Allow-Origin"] in ("*", "null")


# --- session --------------------------------------------------------------


async def test_session_for_administrators_only(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_read_only_access_token: str,
) -> None:
    """A user who is not an administrator gets no session."""
    await _setup(hass, config_entry)
    client = await hass_client(hass_read_only_access_token)
    resp = await client.post(
        "/api/hottoh/session", json={"entry_id": config_entry.entry_id}
    )
    assert resp.status == HTTPStatus.UNAUTHORIZED


async def test_session_needs_authentication(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """No access token, no session."""
    await _setup(hass, config_entry)
    client = await hass_client_no_auth()
    resp = await client.post(
        "/api/hottoh/session", json={"entry_id": config_entry.entry_id}
    )
    assert resp.status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("body", [{"entry_id": "nope"}, {}, [], {"entry_id": 3}])
async def test_session_unknown_stove(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    body: object,
) -> None:
    """Only a stove that is relayed gets a session."""
    await _setup(hass, config_entry)
    client = await hass_client()
    resp = await client.post("/api/hottoh/session", json=body)
    assert resp.status == HTTPStatus.NOT_FOUND


async def test_session_invalid_json(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """A body that is not JSON is refused."""
    await _setup(hass, config_entry)
    client = await hass_client()
    resp = await client.post("/api/hottoh/session", data="{")
    assert resp.status == HTTPStatus.BAD_REQUEST


async def test_session_renewal_keeps_the_url(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """The panel renews its token: same URL, so the interface is not reloaded."""
    await _setup(hass, config_entry)
    client = await hass_client()
    first = await _session(client, config_entry)
    assert first["url"] == f"/api/hottoh/web/{config_entry.entry_id}/{first['token']}/"
    assert first["ttl"] == web.SESSION_TTL_S
    again = await _session(client, config_entry, first["token"])
    assert again["token"] == first["token"]
    # a token that is not (or no longer) valid is replaced
    other = await _session(client, config_entry, "forged")
    assert other["token"] not in ("forged", first["token"])


def test_sessions_are_tied_to_a_user_and_a_stove(monkeypatch) -> None:
    """A token is renewed only by its administrator, and opens only its stove."""
    now = [1000.0]
    monkeypatch.setattr(web.time, "monotonic", lambda: now[0])
    data = web.WebData()
    token = data.open_session("alice", "stove-1")
    assert data.valid(token, "stove-1")
    assert not data.valid(token, "stove-2")
    assert not data.valid("forged", "stove-1")
    # another administrator, or another stove, cannot take the token over
    assert data.open_session("bob", "stove-1", token) != token
    assert data.open_session("alice", "stove-2", token) != token
    # expiry, and renewal that postpones it
    now[0] += web.SESSION_TTL_S - 1
    assert data.open_session("alice", "stove-1", token) == token
    now[0] += web.SESSION_TTL_S - 1
    assert data.valid(token, "stove-1")
    now[0] += 2
    assert not data.valid(token, "stove-1")


# --- relay ----------------------------------------------------------------


async def test_relay(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The sandboxed page sends nothing but the token in the path."""
    await _setup(hass, config_entry)
    session = await _session(await hass_client(), config_entry)
    page = await hass_client_no_auth()  # no access token, no cookie

    resp = await page.get(session["url"] + "api/status", headers=FROM_PAGE)
    assert resp.status == HTTPStatus.OK
    assert json.loads(await resp.read())["connected"] is True
    _assert_sandboxed(resp)

    resp = await page.post(
        session["url"] + "api/dat/set_eco_mode",
        json={"value": False},
        headers=FROM_PAGE,
    )
    assert resp.status == HTTPStatus.OK
    _assert_sandboxed(resp)
    assert bridge.writes[-1] == ("dat/set_eco_mode", {"value": False})


async def test_relay_does_not_forward_the_upstream_policy(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The policy of hottoh_api (`frame-ancestors 'none'`) would forbid the panel's iframe."""
    await _setup(hass, config_entry)
    session = await _session(await hass_client(), config_entry)
    page = await hass_client_no_auth()
    resp = await page.get(session["url"] + "api/status", headers=FROM_PAGE)
    assert "frame-ancestors" not in resp.headers["Content-Security-Policy"]


async def test_relay_refuses_without_a_valid_token(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Forged token, other stove: refused, and the refusal is readable by the page."""
    await _setup(hass, config_entry)
    session = await _session(await hass_client(), config_entry)
    page = await hass_client_no_auth()
    entry_id = config_entry.entry_id
    before = len(bridge.writes)

    for url in (
        f"/api/hottoh/web/{entry_id}/forged/api/status",
        f"/api/hottoh/web/other/{session['token']}/api/status",
    ):
        resp = await page.get(url, headers=FROM_PAGE)
        assert resp.status == HTTPStatus.UNAUTHORIZED, url
        _assert_sandboxed(resp)
    # no token at all: not even a route
    resp = await page.get(f"/api/hottoh/web/{entry_id}//api/status", headers=FROM_PAGE)
    assert resp.status == HTTPStatus.NOT_FOUND
    resp = await page.post(
        f"/api/hottoh/web/{entry_id}/forged/api/dat/set_on_off",
        json={"value": True},
        headers=FROM_PAGE,
    )
    assert resp.status == HTTPStatus.UNAUTHORIZED
    assert len(bridge.writes) == before


async def test_relay_stops_with_the_entry(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """An unloaded stove is not relayed any more, whatever its tokens."""
    await _setup(hass, config_entry)
    session = await _session(await hass_client(), config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    page = await hass_client_no_auth()
    resp = await page.get(session["url"] + "api/status", headers=FROM_PAGE)
    assert resp.status == HTTPStatus.UNAUTHORIZED


async def test_preflight(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The sandboxed interface is cross-origin: its JSON POST is preceded by an OPTIONS."""
    await _setup(hass, config_entry)
    session = await _session(await hass_client(), config_entry)
    page = await hass_client_no_auth()

    resp = await page.options(
        session["url"] + "api/dat/set_eco_mode",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status in (HTTPStatus.OK, HTTPStatus.NO_CONTENT)
    # the origin of a sandboxed page is the string "null"
    assert resp.headers["Access-Control-Allow-Origin"] in ("*", "null")
    assert "content-type" in resp.headers["Access-Control-Allow-Headers"].lower()
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]
    assert not bridge.writes  # a preflight never reaches the stove


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("api/status", True),
        ("ui/app.js", True),
        ("", True),
        ("a/b..c/d", True),
        ("..", False),
        ("../x", False),
        ("a/../x", False),
        ("a/..", False),
        ("./x", False),
        ("a\\..\\x", False),
        ("a\x00b", False),
    ],
)
def test_safe_path(path: str, expected: bool) -> None:
    """Paths that climb out of the interface are refused."""
    assert web.safe_path(path) is expected


async def test_climbing_path_never_reaches_hottoh_api(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock,
) -> None:
    """`..` is refused, even encoded (the client would resolve a plain one itself)."""
    await _setup(hass, config_entry)
    session = await _session(await hass_client(), config_entry)
    page = await hass_client_no_auth()
    aioclient_mock.clear_requests()
    for encoded in ("%2e%2e/api/status", "ui/%2E%2E/api/status", "a%5C..%5Cb"):
        resp = await page.get(
            URL(session["url"] + encoded, encoded=True), headers=FROM_PAGE
        )
        assert resp.status == HTTPStatus.BAD_REQUEST, encoded
    assert not aioclient_mock.mock_calls


async def test_relay_limits_the_body(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A body larger than any request of the interface is not relayed."""
    await _setup(hass, config_entry)
    session = await _session(await hass_client(), config_entry)
    page = await hass_client_no_auth()
    resp = await page.post(
        session["url"] + "api/dat/set_eco_mode",
        data=b"x" * (web.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json", **FROM_PAGE},
    )
    assert resp.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    _assert_sandboxed(resp)
    assert not bridge.writes


async def test_relay_hottoh_api_unreachable(
    hass: HomeAssistant,
    bridge: FakeBridge,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The page reads the failure instead of a CORS error."""
    await _setup(hass, config_entry)
    session = await _session(await hass_client(), config_entry)
    bridge.down = True
    page = await hass_client_no_auth()
    resp = await page.get(session["url"] + "api/status", headers=FROM_PAGE)
    assert resp.status == HTTPStatus.BAD_GATEWAY
    _assert_sandboxed(resp)
