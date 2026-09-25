"""Play back Synology Surveillance Station recordings inside Home Assistant."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
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
from .views import VodInitView, VodManager, VodPlaylistView, VodSegmentView, remove_stale_temp_files

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _version() -> str:
    return json.loads((Path(__file__).parent / "manifest.json").read_text())["version"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the parts shared by all entries: views, WS commands, the card."""
    manager = VodManager(hass)
    if removed := await hass.async_add_executor_job(remove_stale_temp_files):
        _LOGGER.info("Removed %d stale remux scratch files", removed)
    hass.data[DOMAIN] = manager
    for view in (VodPlaylistView, VodInitView, VodSegmentView):
        hass.http.register_view(view(manager))
    websocket.async_register(hass)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), False)]
    )
    version = await hass.async_add_executor_job(_version)
    # Cache-bust on every release so browsers pick up a new card.
    await _register_card(hass, f"{STATIC_URL}/{CARD_FILENAME}?v={version}")
    return True


async def _register_card(hass: HomeAssistant, url: str) -> None:
    """Load the card through a Lovelace resource, kept at the current version.

    Not add_extra_js_url: that bakes the import into index.html, and HA's
    service worker serves index.html stale-while-revalidate, so the mobile app
    can keep showing a page from before the integration was installed
    ("Custom element doesn't exist"). The resource list is fetched over the
    WebSocket every time a dashboard loads. YAML-mode resources can't be
    edited from here, so they fall back to add_extra_js_url.
    """
    resources = getattr(hass.data.get(LOVELACE_DATA), "resources", None)
    if not isinstance(resources, ResourceStorageCollection):
        add_extra_js_url(hass, url)
        return
    await resources.async_get_info()  # loads the collection
    base = url.split("?")[0]
    for item in resources.async_items():
        if item["url"].split("?")[0] == base:
            if item["url"] != url:
                await resources.async_update_item(item["id"], {"res_type": "module", "url": url})
            return
    await resources.async_create_item({"res_type": "module", "url": url})


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
    client.on_auth_failed = lambda: entry.async_start_reauth(hass)
    hass.data[DOMAIN].clients[entry.entry_id] = client
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = hass.data[DOMAIN].clients.pop(entry.entry_id, None)
    if client is not None:
        await client.logout()
    return True
