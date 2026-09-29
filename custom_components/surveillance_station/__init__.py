"""Play back Synology Surveillance Station recordings inside Home Assistant."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from synology_ss_playback import (
    SSAuthError,
    SSError,
    SSInfo,
    SurveillanceStationClient,
    drain_transcodes,
    remove_stale_temp_files,
)

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .const import (
    CARD_FILENAME,
    CONF_FRIGATE,
    CONF_FRIGATE_CAMERAS,
    CONF_FRIGATE_LINK,
    CONF_FRIGATE_OBJECTS,
    CONF_FRIGATE_QUIET,
    CONF_FRIGATE_QUIET_KINDS,
    CONF_FRIGATE_TOPIC,
    CONF_FRIGATE_URL,
    CONF_VERIFY_SSL,
    DEFAULT_FRIGATE_OBJECTS,
    DEFAULT_FRIGATE_QUIET_KINDS,
    DEFAULT_FRIGATE_QUIET_MINUTES,
    DEFAULT_FRIGATE_TOPIC,
    DOMAIN,
    STATIC_URL,
)
from .frigate import DATA_FRIGATE, FrigateBridge, FrigateImageView, FrigateThumbnailView, store_key as frigate_store_key
from .frigate_api import FrigateAPI
from .manager import DATA_MANAGER, VodManager
from .mute import MuteRules, store_key as mute_store_key
from .mute_services import async_setup_services
from .views import (
    LargeImageView,
    LiveStreamView,
    ThumbnailView,
    VodInitView,
    VodPlaylistView,
    VodSegmentView,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [Platform.SWITCH]

type SurveillanceStationConfigEntry = ConfigEntry[SurveillanceStationClient]


def _version() -> str:
    return json.loads((Path(__file__).parent / "manifest.json").read_text())["version"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the parts shared by all entries: views, WS commands, the card."""
    if removed := await hass.async_add_executor_job(remove_stale_temp_files):
        _LOGGER.info("Removed %d stale remux scratch files", removed)
    manager = VodManager(hass)
    await manager.async_load()
    hass.data[DATA_MANAGER] = manager
    for view in (VodPlaylistView, VodInitView, VodSegmentView, ThumbnailView, LargeImageView, LiveStreamView):
        hass.http.register_view(view(manager))
    hass.http.register_view(FrigateImageView(hass, manager))
    hass.http.register_view(FrigateThumbnailView(hass, manager))
    websocket.async_register(hass)
    async_setup_services(hass)

    async def _drain(_: Event) -> None:
        # A running GPU transcode must not be killed by the loop's teardown
        # (see synology_ss_playback.fetch_timelapse_segment).
        await drain_transcodes()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _drain)
    # Cached for a month: every URL of these files carries the version
    # (the resource, the extra-JS URL, each card's imports).
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), True)]
    )
    if _storage_resources(hass) is None:
        # YAML-mode resources can't be edited from here: load it everywhere.
        add_extra_js_url(hass, await _card_url(hass))
    return True


async def _card_url(hass: HomeAssistant) -> str:
    # Cache-bust on every release so browsers pick up a new card.
    version = await hass.async_add_executor_job(_version)
    return f"{STATIC_URL}/{CARD_FILENAME}?v={version}"


def _storage_resources(hass: HomeAssistant) -> ResourceStorageCollection | None:
    resources = getattr(hass.data.get(LOVELACE_DATA), "resources", None)
    return resources if isinstance(resources, ResourceStorageCollection) else None


async def _register_card(hass: HomeAssistant, url: str) -> None:
    """Load the card through a Lovelace resource, kept at the current version.

    Not add_extra_js_url: that bakes the import into index.html, and HA's
    service worker serves index.html stale-while-revalidate, so the mobile app
    can keep showing a page from before the integration was installed
    ("Custom element doesn't exist"). The resource list is fetched over the
    WebSocket every time a dashboard loads. (YAML-mode resources: see
    async_setup.)
    """
    if (resources := _storage_resources(hass)) is None:
        return
    await resources.async_get_info()  # loads the collection
    base = url.split("?")[0]
    for item in resources.async_items():
        if item["url"].split("?")[0] == base:
            if item["url"] != url:
                await resources.async_update_item(item["id"], {"res_type": "module", "url": url})
            return
    await resources.async_create_item({"res_type": "module", "url": url})


async def async_setup_entry(hass: HomeAssistant, entry: SurveillanceStationConfigEntry) -> bool:
    data = entry.data
    client = SurveillanceStationClient(
        async_get_clientsession(hass, verify_ssl=data[CONF_VERIFY_SSL]),
        data[CONF_HOST],
        data[CONF_PORT],
        data[CONF_SSL],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )
    info: SSInfo | None = None
    try:
        await client.login()
        info = await client.info()
    except SSAuthError as err:
        await _logout(client)
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="invalid_auth") from err
    except SSError as err:
        # Unreachable (a NAS rebooting), or an odd answer (2026-09-25).
        await _logout(client)
        if not _knows_its_nas(entry):
            # The NAS's serial is needed first (the entry's unique ID).
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        # Start anyway: the client logs in on first use, so bookmarks and
        # playback work the moment SS answers, rather than after HA's setup
        # backoff (up to 10 minutes after a NAS reboot), during which not
        # even Frigate's reviews would be received.
        _LOGGER.warning(
            "Surveillance Station at %s not usable yet (%s); starting anyway, it is used once it answers",
            data[CONF_HOST], err,
        )
    except Exception as err:
        # A bug: retried, visibly (not started as if all were well); never a
        # setup_error that stays until someone reloads by hand.
        await _logout(client)
        _LOGGER.exception("Unexpected error setting up Surveillance Station; retrying")
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": type(err).__name__},
        ) from err
    if info is not None and entry.unique_id != info.serial:
        # Entries from before 0.5 were keyed by host:port.
        hass.config_entries.async_update_entry(entry, unique_id=info.serial)
    client.on_auth_failed = lambda: entry.async_start_reauth(hass)
    entry.runtime_data = client
    # Per entry rather than in async_setup, so that removing the last entry
    # and adding one back (no restart in between) brings the card back.
    try:
        await _register_card(hass, await _card_url(hass))
    except Exception:
        # The card is a convenience; bookmarks and playback work without it.
        _LOGGER.exception("Could not register the timeline card's Lovelace resource")
    if entry.options.get(CONF_FRIGATE):
        options = entry.options
        mute = MuteRules(hass, entry.entry_id)
        await mute.async_load()
        # Its timer, also if setup fails after this (unloading stops it too).
        entry.async_on_unload(mute.stop)
        bridge = FrigateBridge(
            hass,
            entry.entry_id,
            client,
            hass.data[DATA_MANAGER],
            options.get(CONF_FRIGATE_TOPIC) or DEFAULT_FRIGATE_TOPIC,
            set(options.get(CONF_FRIGATE_OBJECTS) or DEFAULT_FRIGATE_OBJECTS),
            options.get(CONF_FRIGATE_LINK) or "",
            options.get(CONF_FRIGATE_QUIET, DEFAULT_FRIGATE_QUIET_MINUTES),
            set(options.get(CONF_FRIGATE_QUIET_KINDS, DEFAULT_FRIGATE_QUIET_KINDS)),
            options.get(CONF_FRIGATE_CAMERAS) or {},
            FrigateAPI(async_get_clientsession(hass), url) if (url := options.get(CONF_FRIGATE_URL)) else None,
            mute,
        )
        hass.data.setdefault(DATA_FRIGATE, {})[entry.entry_id] = bridge
        # In the background: MQTT may still be starting.
        entry.async_create_background_task(hass, bridge.start(), "surveillance_station frigate setup")
        # The mute switches.
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _knows_its_nas(entry: SurveillanceStationConfigEntry) -> bool:
    """Set up before, with the NAS's serial as its ID (before 0.5: host:port)."""
    return bool(entry.unique_id) and ":" not in entry.unique_id


async def _logout(client: SurveillanceStationClient) -> None:
    try:
        await client.logout()
    except Exception:  # noqa: BLE001 - best effort, never instead of the real error
        _LOGGER.debug("Logout failed", exc_info=True)


async def async_unload_entry(hass: HomeAssistant, entry: SurveillanceStationConfigEntry) -> bool:
    # Whether there are switches to unload is what setup did, not what the
    # options say now (a reload is made after they changed). Those first: if
    # they cannot go, the entry stays loaded, its bridge still running. Then
    # the bridge, so that no bookmark is being made while the client logs
    # out; its state written now, before a reload reads it (or a removal
    # deletes it).
    if (bridge := hass.data.get(DATA_FRIGATE, {}).get(entry.entry_id)) is not None:
        if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
            return False
        hass.data[DATA_FRIGATE].pop(entry.entry_id)
        bridge.stop()
        try:
            await bridge.async_flush()
        except Exception:  # noqa: BLE001 - never a failed unload for it
            _LOGGER.warning("Could not save the Frigate bridge's state", exc_info=True)
        bridge.mute.stop()
        try:
            await bridge.mute.async_flush()
        except Exception:  # noqa: BLE001 - never a failed unload for it
            _LOGGER.warning("Could not save the mute rules", exc_info=True)
    hass.data[DATA_MANAGER].drop_entry(entry.entry_id)
    # Closed rather than logged out: a handler still running can't log it in
    # again (a DSM session nobody would end).
    try:
        await entry.runtime_data.close()
    except Exception:  # noqa: BLE001 - never a failed unload for it
        _LOGGER.debug("Closing the Surveillance Station client failed", exc_info=True)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: SurveillanceStationConfigEntry) -> None:
    """Delete the entry's stored thumbnails and frames; take the card's Lovelace resource away with the last entry."""
    if (manager := hass.data.get(DATA_MANAGER)) is not None:
        await manager.disk.drop_entry(entry.entry_id)
        await manager.disk_large.drop_entry(entry.entry_id)
        await manager.forget_frames(entry.entry_id)
    await Store(hass, 1, frigate_store_key(entry.entry_id)).async_remove()
    await Store(hass, 1, mute_store_key(entry.entry_id)).async_remove()
    if any(e.entry_id != entry.entry_id for e in hass.config_entries.async_entries(DOMAIN)):
        return
    if (resources := _storage_resources(hass)) is None:
        return
    await resources.async_get_info()
    base = f"{STATIC_URL}/{CARD_FILENAME}"
    for item in list(resources.async_items()):
        if item["url"].split("?")[0] == base:
            await resources.async_delete_item(item["id"])
