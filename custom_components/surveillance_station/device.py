"""One device per Surveillance Station camera, under the entry's own device.

A camera is its SS id: a rename changes the device's name and nothing else.
The entry's device (the hub) is made with the entry; a camera's when SS first
lists it, with the entities of every platform for that camera (mute switches,
detections, when its mute ends) on it. A camera SS stops listing loses its
device (its entities with it, and its mute rules) once it has been missing from
two listings, DEVICE_GONE_AFTER seconds apart, so that a short listing costs no
entity_id, area or disabled flag.

Every platform is told the cameras through ``async_on_change`` here rather than
from the bridge: the devices and the entities' unique ids (0.22 named them by
the camera's key) are put right first.
"""

from __future__ import annotations

from collections.abc import Callable
import asyncio
import logging
import re
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry, DeviceInfo

from . import frigate as frigate_mod
from .const import DOMAIN, FRIGATE_CAMERAS_TTL
from .frigate import FrigateBridge, camera_key

if TYPE_CHECKING:
    from . import SurveillanceStationConfigEntry

_LOGGER = logging.getLogger(__name__)

# A camera's device goes once SS has not listed it for this many seconds, over two listings at least.
DEVICE_GONE_AFTER = 1800
# The mute switches of 0.22 (their camera's key after this, then the kind for a kind's) and of now.
_CAMERA_SWITCH = "mute_camera_"
_CAMERA_SWITCH_NOW = re.compile(r"(id_\d+|kind_id_\d+_[a-z]+)\Z")
_CAMERA_SUFFIX = "_camera_"

# Told the cameras listed (id -> name), and the ids whose device has just gone.
CamerasChanged = Callable[[dict[int, str], set[int]], None]


def hub_identifier(entry_id: str) -> tuple[str, str]:
    return (DOMAIN, entry_id)


def camera_identifier(entry_id: str, camera_id: int) -> tuple[str, str]:
    return (DOMAIN, f"{entry_id}{_CAMERA_SUFFIX}{camera_id}")


def camera_id_of(entry_id: str, device: DeviceEntry) -> int | None:
    """The SS camera a device is (None: the entry's own device, or another entry's)."""
    prefix = f"{entry_id}{_CAMERA_SUFFIX}"
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier.startswith(prefix) and identifier[len(prefix) :].isdigit():
            return int(identifier[len(prefix) :])
    return None


def hub_device_info(entry_id: str) -> DeviceInfo:
    return DeviceInfo(identifiers={hub_identifier(entry_id)}, name="Surveillance Station", manufacturer="Synology")


def camera_device_info(entry_id: str, camera_id: int, name: str, link: str = "", hub_id: str | None = None) -> DeviceInfo:
    """A camera's device, under the hub's (its registry id); with the dashboard the entry links to (the option), its page opens the card on this camera."""
    info = DeviceInfo(
        identifiers={camera_identifier(entry_id, camera_id)},
        name=name,
        manufacturer="Synology",
        model="Surveillance Station camera",
    )
    if hub_id is not None:
        info["via_device_id"] = hub_id
    if link.startswith("/") and not link.startswith("//"):
        sep = "&" if "?" in link else "?"
        info["configuration_url"] = f"homeassistant://{link.lstrip('/')}{sep}ss_camera={camera_id}"
    return info


class CameraDevices:
    """Keeps the cameras' devices as SS lists them, and tells the platforms."""

    def __init__(self, hass: HomeAssistant, entry: SurveillanceStationConfigEntry, bridge: FrigateBridge) -> None:
        self.hass = hass
        self.entry = entry
        self.bridge = bridge
        self._callbacks: list[CamerasChanged] = []
        self._cameras: dict[int, str] = {}
        # Cameras' devices, and legacy switches (unique ids), the last listing had no camera for: since when.
        self._absent: dict[int | str, float] = {}

    @callback
    def async_start(self) -> Callable[[], None]:
        """Follow the bridge's listings; the returned callable stops."""
        return self.bridge.async_on_cameras(self._listed)

    @callback
    def async_on_change(self, cb: CamerasChanged) -> Callable[[], None]:
        """Told the cameras each time they are listed (and now, if they have been)."""
        self._callbacks.append(cb)
        if self._cameras:
            cb(dict(self._cameras), set())

        @callback
        def remove() -> None:
            if cb in self._callbacks:
                self._callbacks.remove(cb)

        return remove

    @callback
    def forget(self, camera_id: int) -> None:
        """The user deleted this (unlisted) camera's device: its rules go, and the platforms let its entities go, so that it gets new ones if it returns."""
        self._absent.pop(camera_id, None)
        self._cameras.pop(camera_id, None)
        self.bridge.forget_camera(camera_id)
        for cb in list(self._callbacks):
            cb(dict(self._cameras), {camera_id})

    def camera_device_info(self, camera_id: int, name: str) -> DeviceInfo:
        hub = dr.async_get(self.hass).async_get_device_by_identifier(hub_identifier(self.entry.entry_id), self.entry.entry_id)
        return camera_device_info(self.entry.entry_id, camera_id, name, self.bridge.link, hub.id if hub else None)

    @callback
    def _listed(self, cameras: dict[int, str]) -> None:
        # Unloading: what is not there when the bridge stops is not touched.
        if self.bridge.stopped or self.entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS:
            return
        # A listing without cameras is likelier SS answering oddly (the library skips what it
        # cannot read) than every camera gone: nothing changes.
        if not cameras:
            return
        entry_id = self.entry.entry_id
        devices = dr.async_get(self.hass)
        now = frigate_mod._monotonic()
        self._migrate_switches(cameras)
        for camera_id, name in cameras.items():
            info = self.camera_device_info(camera_id, name)
            devices.async_get_or_create(
                config_entry_id=entry_id,
                identifiers=info["identifiers"],
                name=name,
                manufacturer=info["manufacturer"],
                model=info["model"],
                via_device_id=info.get("via_device_id"),
                configuration_url=info.get("configuration_url"),
            )
        registered = {
            camera_id: device
            for device in dr.async_entries_for_config_entry(devices, entry_id)
            if (camera_id := camera_id_of(entry_id, device)) is not None
        }
        stale: set[int | str] = registered.keys() - cameras.keys()
        stale |= {item.unique_id for item in self._legacy_switches()}  # those _migrate_switches found no camera for
        gone = {key for key in stale & self._absent.keys() if now - self._absent[key] >= DEVICE_GONE_AFTER}
        entities = er.async_get(self.hass)
        for key in gone:
            if isinstance(key, int):
                devices.async_remove_device(registered[key].id)  # its entities go with it
                self.bridge.forget_camera(key)
            elif (entity_id := entities.async_get_entity_id("switch", DOMAIN, key)) is not None:
                entities.async_remove(entity_id)
        self._absent = {key: self._absent.get(key, now) for key in stale - gone}
        self._cameras = dict(cameras)
        for cb in list(self._callbacks):
            cb(dict(cameras), {key for key in gone if isinstance(key, int)})

    def _legacy_switches(self) -> list[er.RegistryEntry]:
        """The switches of 0.22 for a camera: named by its key."""
        prefix = f"{self.entry.entry_id}_{_CAMERA_SWITCH}"
        return [
            item
            for item in er.async_entries_for_config_entry(er.async_get(self.hass), self.entry.entry_id)
            if item.domain == "switch"
            and item.unique_id.startswith(prefix)
            and not _CAMERA_SWITCH_NOW.match(item.unique_id[len(prefix) :])
        ]

    def _migrate_switches(self, cameras: dict[int, str]) -> None:
        """A camera's switches of 0.22 (unique ids by its key) become its own (by its id): same entity_ids, areas, names."""
        ids_by_key = {key: camera_id for camera_id, name in cameras.items() if (key := camera_key(name))}
        registry = er.async_get(self.hass)
        prefix = f"{self.entry.entry_id}_{_CAMERA_SWITCH}"
        for item in self._legacy_switches():
            rest = item.unique_id[len(prefix) :]
            if rest.startswith("kind_"):  # kind_<key>_<kind>; a key has no underscore, nor has a kind
                key, _, kind = rest[len("kind_") :].rpartition("_")
                suffix = "kind_id_{}_" + kind
            else:
                key, suffix = rest, "id_{}"
            if (camera_id := ids_by_key.get(key)) is None:
                continue
            unique_id = prefix + suffix.format(camera_id)
            if registry.async_get_entity_id("switch", DOMAIN, unique_id) is None:
                registry.async_update_entity(item.entity_id, new_unique_id=unique_id)
            else:
                _LOGGER.debug("Not renaming the switch %s: %s exists", item.unique_id, unique_id)


async def list_cameras(bridge: FrigateBridge) -> None:
    """Until SS has answered with its cameras (reviews list them too, as they come).

    Asked again after a minute, then less and less often while SS stays down.
    """
    wait = 60
    while not await bridge.camera_names():
        await asyncio.sleep(wait)
        wait = min(2 * wait, FRIGATE_CAMERAS_TTL)
