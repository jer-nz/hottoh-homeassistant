"""Web interface of hottoh_api in a panel of Home Assistant.

hottoh_api has no authentication. Its interface is relayed by Home Assistant under
`/api/hottoh/web/<entry id>/<token>/`, for administrators only.

The interface is not trusted: it is served from the origin of Home Assistant, where a script
could read the access token kept by the frontend. So it runs in a sandbox, without the origin
of Home Assistant: the panel shows it in an `<iframe sandbox>` (no `allow-same-origin`), and
every answer of the relay carries `Content-Security-Policy: sandbox`, which also covers a page
opened directly. A sandboxed page sends neither cookies nor the access token, and its requests
are cross-origin (`Origin: null`): the relay therefore authenticates with a short-lived token
in the path, tied to one administrator and one stove, and answers with CORS headers. The
interface uses relative paths, so it works under this prefix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
import logging
from pathlib import Path
import secrets
import time
from typing import Any

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

SESSION_TTL_S = 900
WEB_PREFIX = "/api/hottoh/web"
STATIC_URL = "/hottoh_static"
PANEL_ELEMENT = "hottoh-panel"
# what the interface may do in its sandbox: scripts, forms and the download of its exports
SANDBOX_FLAGS = "allow-scripts allow-forms allow-downloads"
# largest body relayed to hottoh_api (its requests are a few hundred bytes)
MAX_BODY_BYTES = 1024 * 1024
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
# added to every answer of the relay, errors included (the CORS headers, for the requests
# of the page, come from Home Assistant: see `cors_allowed`)
SECURITY_HEADERS = {
    "Content-Security-Policy": f"sandbox {SANDBOX_FLAGS}",
    # the token is in the URL: never in a Referer
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@dataclass(slots=True)
class Session:
    """Right of one administrator to the interface of one stove."""

    user_id: str
    entry_id: str
    expires: float


@dataclass(slots=True)
class WebData:
    """Sessions and relay targets, shared by every entry."""

    # token -> session
    sessions: dict[str, Session] = field(default_factory=dict)
    # entry id -> base URL of hottoh_api
    targets: dict[str, str] = field(default_factory=dict)

    def _purge(self, now: float) -> None:
        self.sessions = {t: s for t, s in self.sessions.items() if s.expires > now}

    def open_session(
        self, user_id: str, entry_id: str, token: str | None = None
    ) -> str:
        """Token valid for SESSION_TTL_S: the given one renewed when it is still valid for
        this administrator and stove, else a new one."""
        now = time.monotonic()
        self._purge(now)
        session = self.sessions.get(token) if token else None
        if (
            token is not None
            and session is not None
            and session.user_id == user_id
            and session.entry_id == entry_id
        ):
            session.expires = now + SESSION_TTL_S
            return token
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(user_id, entry_id, now + SESSION_TTL_S)
        return token

    def valid(self, token: str, entry_id: str) -> bool:
        """Token of a session of this stove that has not expired."""
        session = self.sessions.get(token)
        return (
            session is not None
            and session.entry_id == entry_id
            and session.expires > time.monotonic()
        )


def _web_data(hass: HomeAssistant) -> WebData:
    return hass.data[f"{DOMAIN}_web"]


def relay_url(entry_id: str, token: str) -> str:
    """Root of the interface of a stove for a session."""
    return f"{WEB_PREFIX}/{entry_id}/{token}/"


def safe_path(path: str) -> bool:
    """False for a path that climbs out of the interface or is not a plain URL path."""
    return not (
        "\\" in path
        or "\x00" in path
        or any(segment in (".", "..") for segment in path.split("/"))
    )


def _response(status: int, **kwargs: Any) -> web.Response:
    headers = {**SECURITY_HEADERS, **kwargs.pop("headers", {})}
    return web.Response(status=status, headers=headers, **kwargs)


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
        config={"entry_id": entry_id},
    )
    return url_path


def remove_web(hass: HomeAssistant, entry_id: str, url_path: str | None) -> None:
    """Undoes async_add_web."""
    data = _web_data(hass)
    data.targets.pop(entry_id, None)
    data.sessions = {t: s for t, s in data.sessions.items() if s.entry_id != entry_id}
    if url_path is not None:
        frontend.async_remove_panel(hass, url_path, warn_if_unknown=False)


class SessionView(HomeAssistantView):
    """Opens or renews a session on the relay for an administrator."""

    url = "/api/hottoh/session"
    name = "api:hottoh:session"

    async def post(self, request: web.Request) -> web.Response:
        """Body `{"entry_id": ..., "token": ...}` (the token to renew, optional)."""
        user = request["hass_user"]
        if not user.is_admin:
            raise Unauthorized
        try:
            body = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON", HTTPStatus.BAD_REQUEST)
        entry_id = body.get("entry_id") if isinstance(body, dict) else None
        token = body.get("token") if isinstance(body, dict) else None
        data = _web_data(request.app["hass"])
        if not isinstance(entry_id, str) or entry_id not in data.targets:
            return self.json_message("Unknown stove", HTTPStatus.NOT_FOUND)
        token = data.open_session(
            user.id, entry_id, token if isinstance(token, str) else None
        )
        return self.json(
            {"token": token, "url": relay_url(entry_id, token), "ttl": SESSION_TTL_S}
        )


class RelayView(HomeAssistantView):
    """Relays the web interface and API of hottoh_api."""

    url = WEB_PREFIX + "/{entry_id}/{token}/{path:.*}"
    name = "api:hottoh:web"
    # checked here: a sandboxed page cannot send the access token of Home Assistant
    requires_auth = False
    # its requests are cross-origin: Home Assistant answers the CORS preflight (OPTIONS, which
    # carries no credentials and reveals nothing) and the answers carry the headers below
    cors_allowed = True

    async def _relay(
        self, request: web.Request, entry_id: str, token: str, path: str
    ) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        data = _web_data(hass)
        if not data.valid(token, entry_id):
            return _response(HTTPStatus.UNAUTHORIZED, text="Session expired")
        if (base_url := data.targets.get(entry_id)) is None:
            return _response(HTTPStatus.NOT_FOUND)
        if not safe_path(path):
            return _response(HTTPStatus.BAD_REQUEST, text="Invalid path")
        if (request.content_length or 0) > MAX_BODY_BYTES:
            return _response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        headers = {
            h: request.headers[h] for h in REQUEST_HEADERS if h in request.headers
        }
        body = await request.read() if request.body_exists else None
        if body is not None and len(body) > MAX_BODY_BYTES:
            return _response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
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
                        f"{WEB_PREFIX}/{entry_id}/{token}{location}"
                        if location.startswith("/")
                        else location
                    )
                return _response(upstream.status, body=content, headers=out)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Relay to %s/%s failed: %s", base_url, path, err)
            return _response(
                HTTPStatus.BAD_GATEWAY, text=f"hottoh_api unreachable: {err}"
            )

    get = post = put = delete = patch = _relay
