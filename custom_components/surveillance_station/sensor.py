"""When a camera's mute ends, as a sensor on its device.

A timestamp while every kind on the camera is muted until a time; unknown when it is
not muted, or muted until turned off (``forever``: the attribute says which).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SurveillanceStationConfigEntry
from .frigate import DATA_FRIGATE
from .mute import MuteRules

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: SurveillanceStationConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge = hass.data[DATA_FRIGATE][entry.entry_id]
    known: set[int] = set()

    @callback
    def add_cameras(cameras: dict[int, str], gone: set[int]) -> None:
        known.difference_update(gone)
        new = [
            MuteEnds(entry, bridge.mute, camera_id, bridge.devices.camera_device_info(camera_id, name))
            for camera_id, name in cameras.items()
            if camera_id not in known
        ]
        known.update(cameras)
        if new:
            async_add_entities(new)

    entry.async_on_unload(bridge.devices.async_on_change(add_cameras))


class MuteEnds(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "mute_ends"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: SurveillanceStationConfigEntry, mute: MuteRules, camera_id: int, device_info: DeviceInfo) -> None:
        self._mute = mute
        self._camera_id = camera_id
        self._attr_unique_id = f"{entry.entry_id}_mute_ends_{camera_id}"
        self._attr_device_info = device_info

    @callback
    def _update(self) -> None:
        on, until = self._mute.coverage(self._camera_id, None)
        self._attr_native_value = None if until is None else dt_util.utc_from_timestamp(until)
        self._attr_extra_state_attributes: dict[str, Any] = {"forever": on and until is None}

    @callback
    def _rules_changed(self) -> None:
        self._update()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._update()
        self.async_on_remove(self._mute.async_add_listener(self._rules_changed))
