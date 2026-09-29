"""Switches that mute notifications: all of them, one camera's, one kind's, or one kind on one camera.

On: muted until turned off (or, for a timed mute from the mute action, until then:
``muted_until``). The switches follow the hierarchy: a camera's all-kinds shows its
kinds on, every kind of it on shows all-kinds on, and what a wider mute covers shows
on but is ``locked`` (turning it raises an error) until that mute is lifted: everything
muted locks every other switch, a kind muted for every camera locks that kind on each
camera. A detection is also muted by rules a switch does not show as its own.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
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
                new.extend(
                    MuteSwitch(entry, rules, "mute_camera_kind", camera=key, kind=kind, camera_name=name)
                    for kind in MUTE_KINDS
                )
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

    def _all_scope(self) -> bool:
        return self._camera is None and self._kind is None

    def _covers(self) -> list[MuteRule]:
        """The rules that make the switch show on (none: off).

        Beyond its own rule a switch shows what its scope is covered by: a
        camera's kind by that camera's all-kinds rule (or all of it by every
        kind of it), and everything a wider rule mutes stays on.
        """
        rules = self._rules.rules()
        exact = [r for r in rules if r.camera == self._camera and r.kind == self._kind]
        if self._camera is not None and self._kind is not None:
            exact += [r for r in rules if r.kind is None and r.camera in (None, self._camera)]
            exact += [r for r in rules if r.kind == self._kind and r.camera is None]
        elif self._camera is not None:
            per_kind = [r for r in rules if r.camera == self._camera and r.kind is not None]
            if not exact and {r.kind for r in per_kind} >= set(MUTE_KINDS):
                exact = per_kind
        if not self._all_scope() and not (self._camera is not None and self._kind is not None):
            exact += [r for r in rules if r.camera is None and r.kind is None]  # everything muted covers all the rest
        return exact

    def _shown(self) -> tuple[bool, float | None]:
        """Whether the switch shows on, and until when (None: until lifted)."""
        exact = self._covers()
        if not exact:
            return False, None
        ends = [r.until for r in exact]  # None: until lifted
        if self._kind is None:  # all kinds lasts until the first of its kinds ends
            finite = [e for e in ends if e is not None]
            return True, min(finite) if finite else None
        return True, None if None in ends else max(e for e in ends if e is not None)  # a kind, until its longest cover ends

    def _locked(self) -> bool:
        """With everything muted, or the kind muted for every camera, the narrower switches are shown on but cannot be turned."""
        if self._all_scope():
            return False
        rules = self._rules.rules()
        if any(r.camera is None and r.kind is None for r in rules):
            return True
        return self._camera is not None and self._kind is not None and any(r.camera is None and r.kind == self._kind for r in rules)

    def _check_unlocked(self) -> None:
        if self._locked():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="mute_locked")

    @property
    def is_on(self) -> bool:
        return self._shown()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        on, until = self._shown()
        # The scope (which camera, which kind) is for the mute card to lay the switches out by.
        attrs: dict[str, Any] = {
            "camera": self._camera_name,
            "kind": self._kind.lower() if self._kind else None,
            "locked": self._locked(),
        }
        if until is not None:
            local = dt_util.as_local(dt_util.utc_from_timestamp(until))
            return {**attrs, "muted_until": dt_util.utc_from_timestamp(until).isoformat(), "mute_ends": local.strftime("%Y-%m-%d %H:%M")}
        # muted_until is a time or nothing; mute_ends is what a card shows for either.
        return {**attrs, "muted_until": None, "mute_ends": "forever" if on else None}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._rules.async_add_listener(self.async_write_ha_state))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._check_unlocked()
        if self._camera is not None and self._kind is None:  # all kinds: one rule for the camera in place of its kinds'
            self._rules.replace(lambda r: r.camera == self._camera and r.kind is not None, [MuteRule(self._camera, None, None)])
        elif not any(r.until is None for r in self._covers()):  # a timed mute becomes one until turned off
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
        # One kind of a camera: the camera's all-kinds rule gives way to its other kinds.
        whole = next((r for r in self._rules.rules() if r.camera == camera and r.kind is None), None)
        held = {r.kind: r.until for r in self._rules.rules() if r.camera == camera and r.kind is not None}
        # Each other kind keeps the longer of the camera's mute and a rule it already had (None: until lifted).
        others = [
            MuteRule(camera, k, None if whole.until is None or (k in held and held[k] is None) else max(whole.until, held.get(k, 0)))
            for k in MUTE_KINDS
            if k != kind
        ] if whole else []
        self._rules.replace(lambda r: r.camera == camera and r.kind in (kind, None), others)
