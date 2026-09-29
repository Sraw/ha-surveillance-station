"""The mute and unmute actions, and the mute buttons of a notification.

Both act on the mute rules (mute.py) of every entry that bookmarks Frigate
detections. The buttons are the companion app's notification actions that the
shipped blueprint adds: their id names the entry, how long and the camera
(``SS_MUTE:<entry id>:<seconds>:<camera key, empty for all>``), and pressing one
fires ``mobile_app_notification_action``, answered here.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, MUTE_ACTION_MAX_SECONDS, MUTE_ACTION_PREFIX
from .frigate import DATA_FRIGATE, camera_key, kind_of
from .mute import MUTE_MAX_SECONDS, MuteRule, MuteRules

_LOGGER = logging.getLogger(__name__)

SERVICE_MUTE = "mute"
SERVICE_UNMUTE = "unmute"
NOTIFICATION_ACTION_EVENT = "mobile_app_notification_action"

_FILTER = {
    vol.Optional("camera"): cv.string,
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


async def _targets(hass: HomeAssistant, camera: str | None, ruled: bool = False) -> list[tuple[MuteRules, str | None]]:
    """The rules of each entry the call is about, with the camera's key there (None: every camera).

    ruled: also a camera SS no longer lists (removed, renamed) that a rule
    still names, by its old name or key, so that the rule can be lifted.
    """
    bridges = list(hass.data.get(DATA_FRIGATE, {}).values())
    if not bridges:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_frigate")
    if camera is None:
        return [(b.mute, None) for b in bridges]
    keys = await asyncio.gather(*(b.resolve_camera(camera) for b in bridges))
    if ruled:
        old = camera_key(camera)
        keys = [old if key is None and old in {r.camera for r in b.mute.rules()} else key for b, key in zip(bridges, keys)]
    found = [(b.mute, key) for b, key in zip(bridges, keys) if key is not None]
    if not found:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="unknown_camera", translation_placeholders={"camera": camera}
        )
    return found


def _kinds(data: dict[str, Any]) -> list[str] | None:
    kinds = list(dict.fromkeys(normalize_kind(o) for o in data.get("objects") or [] if o.strip()))
    return kinds or None


async def _mute(hass: HomeAssistant, call: ServiceCall) -> None:
    """Mute, setting the end of each rule it names (shorter than before, too)."""
    duration = call.data.get("duration")
    until = None if duration is None else time.time() + duration.total_seconds()
    kinds = _kinds(call.data) or [None]
    for rules, camera in await _targets(hass, call.data.get("camera")):
        rules.replace(lambda r: False, [MuteRule(camera, kind, until) for kind in kinds])  # one change, however many kinds


async def _unmute(hass: HomeAssistant, call: ServiceCall) -> None:
    """Lift the rules for the given camera and/or kinds (each rule of them, whatever else it covers)."""
    kinds = _kinds(call.data)
    for rules, camera in await _targets(hass, call.data.get("camera"), ruled=True):

        def matches(rule: MuteRule, camera: str | None = camera) -> bool:
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
    # Only a camera the entry knows (any event on the bus may say anything).
    if camera and (camera := camera_key(camera)) not in bridge.camera_keys():
        return
    # A button pressed on an old notification must not cut short a longer mute.
    bridge.mute.extend(camera or None, time.time() + seconds_int)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def mute(call: ServiceCall) -> None:
        await _mute(hass, call)

    async def unmute(call: ServiceCall) -> None:
        await _unmute(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_MUTE, mute, MUTE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UNMUTE, unmute, UNMUTE_SCHEMA)

    @callback
    def pressed(event: Event) -> None:
        _notification_action(hass, event)

    hass.bus.async_listen(NOTIFICATION_ACTION_EVENT, pressed)
