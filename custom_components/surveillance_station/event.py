"""A camera's detections as an event entity on its device.

Each detection announced (the ``surveillance_station_detection`` event on the
bus) is also an event of that camera's entity, whatever its objects: they are
attributes (``objects``, ``labels``, ``zones``, ``muted``, ...), as the bus event
has them, without the links and signed image addresses (they would end up in
the recorder). An automation can trigger on the entity, or on the bus event.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SurveillanceStationConfigEntry
from .const import DETECTION_EVENT
from .frigate import DATA_FRIGATE

PARALLEL_UPDATES = 0
EVENT_TYPE = "detection"
# What of the bus event the entity carries (the rest is links and signed addresses).
ATTRIBUTES = ("review_id", "bookmark_id", "objects", "labels", "zones", "severity", "muted", "frigate_camera", "start")


async def async_setup_entry(
    hass: HomeAssistant, entry: SurveillanceStationConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge = hass.data[DATA_FRIGATE][entry.entry_id]
    entities: dict[int, DetectionEvent] = {}

    @callback
    def add_cameras(cameras: dict[int, str], gone: set[int]) -> None:
        for camera_id in gone:
            entities.pop(camera_id, None)
        new = [
            entities.setdefault(camera_id, DetectionEvent(entry, camera_id, bridge.devices.camera_device_info(camera_id, name)))
            for camera_id, name in cameras.items()
            if camera_id not in entities
        ]
        if new:
            async_add_entities(new)

    @callback
    def detected(event: Event) -> None:
        if event.data.get("entry_id") == entry.entry_id and (entity := entities.get(event.data.get("camera_id"))) is not None:
            entity.detected(event.data)

    entry.async_on_unload(hass.bus.async_listen(DETECTION_EVENT, detected))
    entry.async_on_unload(bridge.devices.async_on_change(add_cameras))


class DetectionEvent(EventEntity):
    _attr_has_entity_name = True
    _attr_translation_key = EVENT_TYPE
    _attr_device_class = EventDeviceClass.MOTION
    _attr_event_types = [EVENT_TYPE]

    def __init__(self, entry: SurveillanceStationConfigEntry, camera_id: int, device_info: DeviceInfo) -> None:
        self._attr_unique_id = f"{entry.entry_id}_detection_{camera_id}"
        self._attr_device_info = device_info

    @callback
    def detected(self, data: dict[str, Any]) -> None:
        if self.hass is None:  # not added yet (or removed)
            return
        self._trigger_event(EVENT_TYPE, {key: data[key] for key in ATTRIBUTES if key in data})
        self.async_write_ha_state()
