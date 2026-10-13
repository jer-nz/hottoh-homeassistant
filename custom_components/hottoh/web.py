"""Web interface of hottoh_api in a panel of Home Assistant.

hottoh_api has no authentication. Its interface is relayed by Home Assistant under
`/api/hottoh/web/<entry id>/`, for administrators only. A page in an iframe cannot send the
access token of Home Assistant, so the panel first asks for a short session (`POST
/api/hottoh/session`, authenticated), kept in an HttpOnly cookie limited to the relay, as the
ingress of add-ons does. The interface uses relative paths, so it works under this prefix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
import logging
from pathlib import Path
import secrets
import time

import aiohttp
from aiohttp import hdrs, web
from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import slugify

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SESSION_COOKIE = "hottoh_session"
SESSION_TTL_S = 900
WEB_PREFIX = "/api/hottoh/web"
STATIC_URL = "/hottoh_static"
PANEL_ELEMENT = "hottoh-panel"
# a write of hottoh_api answers at once; the clock write waits up to a minute
RELAY_TIMEOUT = aiohttp.ClientTimeout(total=90)
# request and answer headers passed through the relay
REQUEST_HEADERS = (hdrs.ACCEPT, hdrs.ACCEPT_LANGUAGE, hdrs.CONTENT_TYPE)
RESPONSE_HEADERS = (
    hdrs.CONTENT_TYPE,
    hdrs.CACHE_CONTROL,
    hdrs.LAST_MODIFIED,
    hdrs.ETAG,
)


@dataclass(slots=True)
class WebData:
    """Sessions and relay targets, shared by every entry."""

    sessions: dict[str, float] = field(default_factory=dict)
    # entry id -> base URL of hottoh_api
    targets: dict[str, str] = field(default_factory=dict)

    def new_session(self) -> str:
        """Session token valid for SESSION_TTL_S."""
        now = time.monotonic()
        self.sessions = {t: exp for t, exp in self.sessions.items() if exp > now}
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + SESSION_TTL_S
        return token

    def valid(self, token: str | None) -> bool:
        """Token of a session that has not expired."""
        return token is not None and self.sessions.get(token, 0) > time.monotonic()


def _web_data(hass: HomeAssistant) -> WebData:
    return hass.data[f"{DOMAIN}_web"]


async def async_setup_web(hass: HomeAssistant) -> None:
    """Views and panel script, once for the integration."""
    hass.data[f"{DOMAIN}_web"] = WebData()
    hass.http.register_view(SessionView())
    hass.http.register_view(RelayView())
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                STATIC_URL, str(Path(__file__).parent / "frontend"), cache_headers=False
            )
        ]
    )


def panel_url_path(hass: HomeAssistant, entry_id: str, title: str) -> str:
    """URL of the panel of a stove, from its name (`/hottoh-poele`)."""
    base = f"{DOMAIN}-{slugify(title, separator='-') or entry_id.lower()}"
    panels = hass.data.get(frontend.DATA_PANELS, {})
    path, number = base, 2
    while path in panels:
        path = f"{base}-{number}"
        number += 1
    return path


async def async_add_web(
    hass: HomeAssistant, entry_id: str, title: str, base_url: str, panel: bool
) -> str | None:
    """Relays hottoh_api of an entry; adds its panel and returns its URL path."""
    _web_data(hass).targets[entry_id] = base_url
    if not panel:
        return None
    url_path = panel_url_path(hass, entry_id, title)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=url_path,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title=title,
        sidebar_icon="mdi:fireplace",
        module_url=f"{STATIC_URL}/panel.js",
        embed_iframe=False,
        require_admin=True,
        config={"relay": f"{WEB_PREFIX}/{entry_id}/"},
    )
    return url_path


def remove_web(hass: HomeAssistant, entry_id: str, url_path: str | None) -> None:
    """Undoes async_add_web."""
    _web_data(hass).targets.pop(entry_id, None)
    if url_path is not None:
        frontend.async_remove_panel(hass, url_path, warn_if_unknown=False)


class SessionView(HomeAssistantView):
    """Opens a session on the relay for an administrator."""

    url = "/api/hottoh/session"
    name = "api:hottoh:session"

    async def post(self, request: web.Request) -> web.Response:
        """Sets the session cookie."""
        if not request["hass_user"].is_admin:
            raise Unauthorized
        token = _web_data(request.app["hass"]).new_session()
        response = self.json({"ttl": SESSION_TTL_S})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL_S,
            path=WEB_PREFIX,
            httponly=True,
            samesite="Strict",
            secure=request.secure,
        )
        return response


class RelayView(HomeAssistantView):
    """Relays the web interface and API of hottoh_api."""

    url = WEB_PREFIX + "/{entry_id}/{path:.*}"
    name = "api:hottoh:web"
    # checked here: the session cookie replaces the access token
    requires_auth = False

    async def _relay(
        self, request: web.Request, entry_id: str, path: str
    ) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        data = _web_data(hass)
        if not data.valid(request.cookies.get(SESSION_COOKIE)):
            return web.Response(status=HTTPStatus.UNAUTHORIZED, text="Session expired")
        if (base_url := data.targets.get(entry_id)) is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        headers = {
            h: request.headers[h] for h in REQUEST_HEADERS if h in request.headers
        }
        body = await request.read() if request.body_exists else None
        try:
            async with async_get_clientsession(hass).request(
                request.method,
                f"{base_url}/{path}",
                params=request.query,
                data=body,
                headers=headers,
                allow_redirects=False,
                timeout=RELAY_TIMEOUT,
            ) as upstream:
                content = await upstream.read()
                out = {
                    h: upstream.headers[h]
                    for h in RESPONSE_HEADERS
                    if h in upstream.headers
                }
                if location := upstream.headers.get(hdrs.LOCATION):
                    # absolute paths of hottoh_api are relative to the relay
                    out[hdrs.LOCATION] = (
                        f"{WEB_PREFIX}/{entry_id}{location}"
                        if location.startswith("/")
                        else location
                    )
                return web.Response(status=upstream.status, body=content, headers=out)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Relay to %s/%s failed: %s", base_url, path, err)
            return web.Response(
                status=HTTPStatus.BAD_GATEWAY, text=f"hottoh_api unreachable: {err}"
            )

    get = post = put = delete = patch = _relay
