"""Switches that mute notifications: all of them, one camera's, or one kind's.

On: muted until turned off. A switch shows the rule for exactly its own scope
(a camera muted for 2 hours by the mute action shows on, with ``muted_until``);
a detection is also muted by rules the switch does not show (all muted, but
this camera's switch is off).
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SurveillanceStationConfigEntry
from .const import DOMAIN, FRIGATE_CAMERAS_TTL, MUTE_KINDS
from .frigate import DATA_FRIGATE, FrigateBridge, camera_key
from .mute import MuteRule, MuteRules

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: SurveillanceStationConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge = hass.data[DATA_FRIGATE][entry.entry_id]
    rules = bridge.mute
    async_add_entities(
        [
            MuteSwitch(entry, rules, "mute_all"),
            *(MuteSwitch(entry, rules, f"mute_{kind.lower()}", kind=kind) for kind in MUTE_KINDS),
        ]
    )
    known: set[str] = set()

    @callback
    def add_cameras(names: list[str]) -> None:
        new = []
        for name in names:
            if bridge.stopped:
                return
            if (key := camera_key(name)) and key not in known:  # a name of only symbols has no key to mute by
                known.add(key)
                new.append(MuteSwitch(entry, rules, "mute_camera", camera=key, camera_name=name))
        if new:
            async_add_entities(new)

    entry.async_on_unload(bridge.async_on_cameras(add_cameras))
    entry.async_create_background_task(hass, _list_cameras(bridge), "surveillance_station mute switches")


async def _list_cameras(bridge: FrigateBridge) -> None:
    """Until SS has answered with its cameras (reviews list them too, as they come)."""
    while not await bridge.camera_names():
        await asyncio.sleep(min(60, FRIGATE_CAMERAS_TTL))


class MuteSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: SurveillanceStationConfigEntry,
        rules: MuteRules,
        key: str,
        camera: str | None = None,
        kind: str | None = None,
        camera_name: str | None = None,
    ) -> None:
        self._rules = rules
        self._camera = camera
        self._kind = kind
        self._attr_translation_key = key
        if camera_name is not None:
            self._attr_translation_placeholders = {"camera": camera_name}
        self._attr_unique_id = f"{entry.entry_id}_{key}" + (f"_{camera}" if camera else "")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Surveillance Station", manufacturer="Synology"
        )

    def _rule(self) -> MuteRule | None:
        return next((r for r in self._rules.rules() if r.camera == self._camera and r.kind == self._kind), None)

    @property
    def is_on(self) -> bool:
        return self._rule() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rule = self._rule()
        until = rule.until if rule is not None else None
        return {"muted_until": None if until is None else dt_util.utc_from_timestamp(until).isoformat()}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._rules.async_add_listener(self.async_write_ha_state))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._rules.add(self._camera, self._kind, None)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._rules.remove(lambda r: r.camera == self._camera and r.kind == self._kind)
