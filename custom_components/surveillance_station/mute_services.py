"""The mute and unmute actions, and the mute buttons of a notification.

Both act on the mute rules (mute.py) of every entry that bookmarks Frigate
detections. The buttons are the companion app's notification actions that the
shipped blueprint adds: their id names the entry, how long and the camera
(``SS_MUTE:<entry id>:<seconds>:<camera key, empty for all>``, the key as in the notification), and pressing one
fires ``mobile_app_notification_action``, answered here.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from functools import partial
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import DOMAIN, MUTE_ACTION_MAX_SECONDS, MUTE_ACTION_PREFIX
from .device import camera_id_of
from .frigate import DATA_FRIGATE, camera_key, kind_of
from .mute import MUTE_MAX_SECONDS, MuteRule, MuteRules

_LOGGER = logging.getLogger(__name__)

SERVICE_MUTE = "mute"
SERVICE_UNMUTE = "unmute"
NOTIFICATION_ACTION_EVENT = "mobile_app_notification_action"

_FILTER = {
    vol.Optional("camera"): cv.string,
    vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("objects"): vol.All(cv.ensure_list, [cv.string]),
}
MUTE_SCHEMA = vol.Schema(
    {
        vol.Optional("duration"): vol.All(
            cv.positive_time_period, vol.Range(min=timedelta(seconds=1), max=timedelta(seconds=MUTE_MAX_SECONDS))
        ),
        **_FILTER,
    }
)
UNMUTE_SCHEMA = vol.Schema(_FILTER)


def normalize_kind(label: str) -> str:
    """A label or kind as the event names it: person, "Person" -> Person; dog -> Animal."""
    return kind_of(label.strip().casefold().replace(" ", "_"))


async def _targets(
    hass: HomeAssistant, camera: str | None, device_ids: list[str] | None = None
) -> list[tuple[MuteRules, int | None]]:
    """The rules of each entry the call is about, with the camera's SS id there (None: every camera).

    Cameras are named (``camera``: an SS or Frigate name) or are devices of the
    integration (``device_id``); given both, the call is about each. One that
    is none is an error, however many others there are.
    """
    bridges = hass.data.get(DATA_FRIGATE, {})
    if not bridges:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_frigate")
    if camera is None and not device_ids:
        return [(b.mute, None) for b in bridges.values()]
    found: list[tuple[MuteRules, int | None]] = []
    if camera is not None:
        ids = await asyncio.gather(*(b.resolve_camera(camera) for b in bridges.values()))
        named = [(b.mute, id_) for b, id_ in zip(bridges.values(), ids) if id_ is not None]
        if not named:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_camera", translation_placeholders={"camera": camera}
            )
        found += named
    devices = dr.async_get(hass)
    for device_id in device_ids or []:
        device = devices.async_get(device_id)
        ours = [
            (bridge.mute, id_)
            for entry_id, bridge in bridges.items()
            if device is not None and (id_ := camera_id_of(entry_id, device)) is not None
        ]
        if not ours:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_a_camera_device")
        found += ours
    return found


def _kinds(data: dict[str, Any]) -> list[str] | None:
    kinds = list(dict.fromkeys(normalize_kind(o) for o in data.get("objects") or [] if o.strip()))
    return kinds or None


async def _mute(hass: HomeAssistant, call: ServiceCall) -> None:
    """Mute, setting the end of each rule it names (shorter than before, too)."""
    duration = call.data.get("duration")
    until = None if duration is None else time.time() + duration.total_seconds()
    kinds = _kinds(call.data) or [None]
    for rules, camera in await _targets(hass, call.data.get("camera"), call.data.get("device_id")):
        rules.replace(lambda r: False, [MuteRule(camera, kind, until) for kind in kinds])  # one change, however many kinds


async def _unmute(hass: HomeAssistant, call: ServiceCall) -> None:
    """Lift the rules for the given camera and/or kinds (each rule of them, whatever else it covers)."""
    kinds = _kinds(call.data)
    for rules, camera in await _targets(hass, call.data.get("camera"), call.data.get("device_id")):

        def matches(rule: MuteRule, camera: int | None = camera) -> bool:
            return (camera is None or rule.camera == camera) and (kinds is None or rule.kind in kinds)

        rules.remove(matches)


@callback
def _notification_action(hass: HomeAssistant, event: Event) -> None:
    action = event.data.get("action")
    if not isinstance(action, str) or not action.startswith(f"{MUTE_ACTION_PREFIX}:"):
        return
    try:
        _, entry_id, seconds, camera = action.split(":", 3)
        seconds_int = int(seconds)
    except ValueError:
        _LOGGER.warning("Ignoring the malformed notification action %r", action)
        return
    bridge = hass.data.get(DATA_FRIGATE, {}).get(entry_id)
    if bridge is None or not 0 < seconds_int <= MUTE_ACTION_MAX_SECONDS:
        return
    camera_id = None
    if camera:
        key = camera_key(camera)
        # Only a camera the entry lists (any event on the bus may say anything); one of only symbols has no key.
        if not key or (camera_id := bridge.camera_of_key(key)) is None:
            _LOGGER.debug("Ignoring the notification action %r: no such camera", action)
            return
    # A button pressed on an old notification must not cut short a longer mute.
    bridge.mute.extend(camera_id, time.time() + seconds_int)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(DOMAIN, SERVICE_MUTE, partial(_mute, hass), MUTE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UNMUTE, partial(_unmute, hass), UNMUTE_SCHEMA)
    hass.bus.async_listen(NOTIFICATION_ACTION_EVENT, partial(_notification_action, hass))
