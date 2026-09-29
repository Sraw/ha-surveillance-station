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
# Every per-camera switch's unique id starts with this (after the entry id).
CAMERA_ID_PREFIX = "mute_camera_"


async def async_setup_entry(
    hass: HomeAssistant, entry: SurveillanceStationConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge = hass.data[DATA_FRIGATE][entry.entry_id]
    mute = bridge.mute
    async_add_entities([MuteSwitch(entry, mute, scope) for scope in (AllScope(), *(KindScope(kind) for kind in MUTE_KINDS))])
    registry = er.async_get(hass)
    per_camera = f"{entry.entry_id}_{CAMERA_ID_PREFIX}"
    known: set[str] = set()

    @callback
    def add_cameras(names: list[str]) -> None:
        # Unloading: the switches go before the bridge stops (see async_unload_entry).
        if bridge.stopped or entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS:
            return
        listed = {key: name for name in names if (key := camera_key(name))}  # a name of only symbols has no key to mute by
        # A camera SS no longer lists (removed, or renamed: a new key) loses its
        # switches; its rules stay (unmute takes its old name).
        wanted = {scope.unique_id(entry.entry_id) for key in listed for scope in camera_scopes(key)}
        for item in er.async_entries_for_config_entry(registry, entry.entry_id):
            if item.domain == "switch" and item.unique_id.startswith(per_camera) and item.unique_id not in wanted:
                registry.async_remove(item.entity_id)
        known.intersection_update(listed)
        new = []
        for key, name in listed.items():
            if key not in known:
                known.add(key)
                new.extend(MuteSwitch(entry, mute, scope, camera_name=name) for scope in camera_scopes(key))
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


class Scope:
    """What a switch mutes: every camera or one (camera, a camera key), every kind or one (kind)."""

    camera: str | None = None
    kind: str | None = None
    # Muted, it locks the switch: shown on, not to be turned (None: nothing does).
    wider: Scope | None = None
    translation_key: str
    unique_suffix: str

    def unique_id(self, entry_id: str) -> str:
        return f"{entry_id}_{self.unique_suffix}"

    def turn_on(self, mute: MuteRules) -> None:
        on, until = mute.coverage(self.camera, self.kind)
        if not on or until is not None:  # a timed mute becomes one until turned off
            mute.add(self.camera, self.kind, None)

    def turn_off(self, mute: MuteRules) -> None:
        """Everything, or a kind for every camera: that rule."""
        mute.remove(lambda r: r.camera == self.camera and r.kind == self.kind)


class AllScope(Scope):
    translation_key = unique_suffix = "mute_all"


class KindScope(Scope):
    """One kind on every camera."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.wider = AllScope()
        self.translation_key = self.unique_suffix = f"mute_{kind.lower()}"


class CameraScope(Scope):
    """Every kind on one camera."""

    translation_key = "mute_camera"

    def __init__(self, camera: str) -> None:
        self.camera = camera
        self.wider = AllScope()
        self.unique_suffix = f"{CAMERA_ID_PREFIX}{camera}"

    def turn_on(self, mute: MuteRules) -> None:
        """One rule for the camera in place of its kinds'."""
        mute.replace(lambda r: r.camera == self.camera and r.kind is not None, [MuteRule(self.camera, None, None)])

    def turn_off(self, mute: MuteRules) -> None:
        """Whatever mutes the camera, its kinds included."""
        mute.remove(lambda r: r.camera == self.camera)


class CameraKindScope(Scope):
    """One kind on one camera."""

    def __init__(self, camera: str, kind: str) -> None:
        self.camera, self.kind = camera, kind
        self.wider = KindScope(kind)
        self.translation_key = f"mute_camera_{kind.lower()}"
        # Camera keys have no underscore, so this cannot clash with another camera's.
        self.unique_suffix = f"{CAMERA_ID_PREFIX}kind_{camera}_{kind.lower()}"

    def turn_off(self, mute: MuteRules) -> None:
        """The camera's all-kinds rule leaves the kind out, keeping its end, so every other kind stays muted."""
        whole = next((r for r in mute.rules() if r.camera == self.camera and r.kind is None), None)
        narrowed = [] if whole is None or self.kind in whole.excluded else [replace(whole, excluded=whole.excluded | {self.kind})]
        mute.replace(lambda r: r.camera == self.camera and r.kind == self.kind, narrowed)


def camera_scopes(camera: str) -> list[Scope]:
    """A camera's switches: all kinds, and each kind."""
    return [CameraScope(camera), *(CameraKindScope(camera, kind) for kind in MUTE_KINDS)]


class MuteSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: SurveillanceStationConfigEntry, mute: MuteRules, scope: Scope, camera_name: str | None = None
    ) -> None:
        self._mute = mute
        self._scope = scope
        self._camera_name = camera_name
        self._attr_translation_key = scope.translation_key
        if camera_name is not None:
            self._attr_translation_placeholders = {"camera": camera_name}
        self._attr_unique_id = scope.unique_id(entry.entry_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Surveillance Station", manufacturer="Synology"
        )

    def _locked(self) -> bool:
        """With everything muted, or the kind muted for every camera, the narrower switches are shown on but cannot be turned."""
        wider = self._scope.wider
        return wider is not None and self._mute.coverage(wider.camera, wider.kind)[0]

    def _check_unlocked(self) -> None:
        if self._locked():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="mute_locked")

    @callback
    def _update(self) -> None:
        """The state and attributes, worked out once per change of the rules."""
        on, until = self._mute.coverage(self._scope.camera, self._scope.kind)
        self._attr_is_on = on
        # The scope (which camera, which kind) is for the mute card to lay the switches out by;
        # muted_until is None when off or until lifted.
        self._attr_extra_state_attributes = {
            "camera": self._camera_name,
            "kind": self._scope.kind.lower() if self._scope.kind else None,
            "locked": self._locked(),
            "muted_until": None if until is None else dt_util.utc_from_timestamp(until).isoformat(),
        }

    @callback
    def _rules_changed(self) -> None:
        self._update()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._update()
        self.async_on_remove(self._mute.async_add_listener(self._rules_changed))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._check_unlocked()
        self._scope.turn_on(self._mute)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._check_unlocked()
        self._scope.turn_off(self._mute)
