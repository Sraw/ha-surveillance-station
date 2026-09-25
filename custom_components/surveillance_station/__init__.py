"""Play back Synology Surveillance Station recordings inside Home Assistant."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .api import SSAuthError, SSError, SurveillanceStationClient
from .const import CARD_FILENAME, CONF_VERIFY_SSL, DOMAIN, STATIC_URL
from .views import VodInitView, VodManager, VodPlaylistView, VodSegmentView

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _version() -> str:
    return json.loads((Path(__file__).parent / "manifest.json").read_text())["version"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the parts shared by all entries: views, WS commands, the card."""
    manager = VodManager(hass)
    hass.data[DOMAIN] = manager
    for view in (VodPlaylistView, VodInitView, VodSegmentView):
        hass.http.register_view(view(manager))
    websocket.async_register(hass)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), False)]
    )
    version = await hass.async_add_executor_job(_version)
    # Cache-bust on every release so browsers pick up a new card.
    add_extra_js_url(hass, f"{STATIC_URL}/{CARD_FILENAME}?v={version}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    client = SurveillanceStationClient(
        async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, False)),
        data[CONF_HOST],
        data[CONF_PORT],
        data.get(CONF_SSL, False),
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )
    try:
        await client.login()
    except SSAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except SSError as err:
        raise ConfigEntryNotReady(str(err)) from err
    hass.data[DOMAIN].clients[entry.entry_id] = client
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = hass.data[DOMAIN].clients.pop(entry.entry_id, None)
    if client is not None:
        await client.logout()
    return True
