"""Switches that mute notifications: all of them, one camera's, one kind's, or one kind on one camera.

On: muted until turned off (or, for a timed mute from the mute action, until then:
``muted_until``). A switch shows on exactly when every detection in its scope is
muted (MuteRules.coverage), so the switches follow the hierarchy: a camera's
all-kinds shows its kinds on, and shows on itself only when every kind there is
muted (the kinds without a switch too); turning one kind of it off leaves every
other kind muted. What a wider mute covers shows on but is ``locked`` (turning it
raises an error) until that mute is lifted: everything muted locks every other
switch, a kind muted for every camera locks that kind on each camera. A
detection is also muted by rules a switch does not show as its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
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
    registry = er.async_get(hass)
    per_camera = f"{entry.entry_id}_mute_camera_"
    known: set[str] = set()

    @callback
    def add_cameras(names: list[str]) -> None:
        # Unloading: the switches go before the bridge stops (see async_unload_entry).
        if bridge.stopped or entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS:
            return
        listed = {key: name for name in names if (key := camera_key(name))}  # a name of only symbols has no key to mute by
        # A camera SS no longer lists (removed, or renamed: a new key) loses its
        # switches; its rules stay (unmute takes its old name).
        wanted = {f"{per_camera}{key}" for key in listed} | {
            f"{per_camera}kind_{key}_{kind.lower()}" for key in listed for kind in MUTE_KINDS
        }
        for item in er.async_entries_for_config_entry(registry, entry.entry_id):
            if item.domain == "switch" and item.unique_id.startswith(per_camera) and item.unique_id not in wanted:
                registry.async_remove(item.entity_id)
        known.intersection_update(listed)
        new = []
        for key, name in listed.items():
            if key not in known:
                known.add(key)
                new.append(MuteSwitch(entry, rules, "mute_camera", camera=key, camera_name=name))
                new.extend(
                    MuteSwitch(entry, rules, "mute_camera_kind", camera=key, kind=kind, camera_name=name)
                    for kind in MUTE_KINDS
                )
        if new:
            async_add_entities(new)

    entry.async_on_unload(bridge.async_on_cameras(add_cameras))
    entry.async_create_background_task(hass, _list_cameras(bridge), "surveillance_station mute switches")


async def _list_cameras(bridge: FrigateBridge) -> None:
    """Until SS has answered with its cameras (reviews list them too, as they come).

    Asked again after a minute, then less and less often while SS stays down.
    """
    wait = 60
    while not await bridge.camera_names():
        await asyncio.sleep(wait)
        wait = min(2 * wait, FRIGATE_CAMERAS_TTL)


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
        self._camera_name = camera_name
        self._attr_translation_key = f"mute_camera_{kind.lower()}" if camera and kind else key
        if camera_name is not None:
            self._attr_translation_placeholders = {"camera": camera_name}
        self._attr_unique_id = f"{entry.entry_id}_{key}" + (f"_{camera}" if camera else "")
        if camera and kind:  # camera keys have no underscore, so this cannot clash with another camera's
            self._attr_unique_id += f"_{kind.lower()}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Surveillance Station", manufacturer="Synology"
        )

    def _locked(self) -> bool:
        """With everything muted, or the kind muted for every camera, the narrower switches are shown on but cannot be turned."""
        if self._camera is None and self._kind is None:
            return False
        return self._rules.coverage(None, self._kind if self._camera is not None else None)[0]

    def _check_unlocked(self) -> None:
        if self._locked():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="mute_locked")

    @callback
    def _update(self) -> None:
        """The state and attributes, worked out once per change of the rules."""
        on, until = self._rules.coverage(self._camera, self._kind)
        self._attr_is_on = on
        # The scope (which camera, which kind) is for the mute card to lay the switches out by;
        # muted_until is None when off or until lifted.
        self._attr_extra_state_attributes = {
            "camera": self._camera_name,
            "kind": self._kind.lower() if self._kind else None,
            "locked": self._locked(),
            "muted_until": None if until is None else dt_util.utc_from_timestamp(until).isoformat(),
        }

    @callback
    def _rules_changed(self) -> None:
        self._update()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._update()
        self.async_on_remove(self._rules.async_add_listener(self._rules_changed))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._check_unlocked()
        if self._camera is not None and self._kind is None:  # all kinds: one rule for the camera in place of its kinds'
            self._rules.replace(lambda r: r.camera == self._camera and r.kind is not None, [MuteRule(self._camera, None, None)])
            return
        on, until = self._rules.coverage(self._camera, self._kind)
        if not on or until is not None:  # a timed mute becomes one until turned off
            self._rules.add(self._camera, self._kind, None)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._check_unlocked()
        camera, kind = self._camera, self._kind
        if camera is not None and kind is None:  # all kinds: whatever mutes the camera, its kinds included
            self._rules.remove(lambda r: r.camera == camera)
            return
        if camera is None:  # everything, or a kind for every camera: that rule
            self._rules.remove(lambda r: r.camera is None and r.kind == kind)
            return
        # One kind of a camera: the camera's all-kinds rule leaves it out, keeping its end, so every other kind stays muted.
        whole = next((r for r in self._rules.rules() if r.camera == camera and r.kind is None), None)
        narrowed = [replace(whole, excluded=whole.excluded | {kind})] if whole is not None and kind not in whole.excluded else []
        self._rules.replace(lambda r: r.camera == camera and r.kind == kind, narrowed)
