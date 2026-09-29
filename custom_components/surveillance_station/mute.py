"""Muting notifications: rules saying which detections are announced as muted.

A rule is a camera (or every camera), a kind (or every kind) and an end (or
none: until it is lifted). A detection is muted when each kind in it is covered
by some rule for its camera: a dog and a person at once, with only animals
muted, is not. Muting affects the notification only: the detection is still
bookmarked, and its event is still fired, with ``muted: true`` for an automation
to respect (the shipped blueprint does).

The cameras are named by ``camera_key`` (a name without case, spaces or
punctuation), the kinds as the event names them (Person, Car, Animal, ...).
Rules are kept across restarts and dropped when they end.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import math
import time
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MUTE_RULES_MAX = 64
# Beyond this a rule is not a mute (and a date past year 9999 breaks the timer).
MUTE_MAX_SECONDS = 3650 * 86400


@dataclass(frozen=True)
class MuteRule:
    camera: str | None  # camera_key; None: every camera
    kind: str | None  # None: every kind
    until: float | None  # epoch seconds; None: until it is lifted

    def covers(self, camera: str, kind: str) -> bool:
        return self.camera in (None, camera) and self.kind in (None, kind)

    def active(self, now: float) -> bool:
        return self.until is None or self.until > now


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.mute.{entry_id}"


class MuteRules:
    """One config entry's mute rules."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._store: Store[dict[str, Any]] = Store(hass, 1, store_key(entry_id))
        self._rules: list[MuteRule] = []
        self._listeners: list[Callable[[], None]] = []
        self._timer: CALLBACK_TYPE | None = None
        self._stopped = False

    async def async_load(self) -> None:
        try:
            stored = await self._store.async_load() or {}
            now = time.time()
            for item in stored.get("rules") or []:
                rule = MuteRule(
                    None if item.get("camera") is None else str(item["camera"]),
                    None if item.get("kind") is None else str(item["kind"]),
                    None if item.get("until") is None else float(item["until"]),
                )
                if rule.until is not None and not math.isfinite(rule.until):
                    continue
                if rule.active(now) and (rule.until is None or rule.until - now <= MUTE_MAX_SECONDS) and rule not in self._rules:
                    self._rules.append(rule)
        except (ValueError, TypeError, AttributeError, HomeAssistantError):
            _LOGGER.warning("Ignoring unreadable mute rules %s", store_key(self.entry_id))
            self._rules = []
        self._rules = self._rules[:MUTE_RULES_MAX]
        self._reschedule()

    def rules(self, now: float | None = None) -> list[MuteRule]:
        """The rules in force (ended ones are dropped by the timer, but not relied on)."""
        now = time.time() if now is None else now
        return [r for r in self._rules if r.active(now)]

    def is_muted(self, camera: str, kinds: list[str], now: float | None = None) -> bool:
        rules = self.rules(now)
        if not kinds:
            return any(r.kind is None and r.camera in (None, camera) for r in rules)
        return all(any(r.covers(camera, k) for r in rules) for k in kinds)

    def add(self, camera: str | None, kind: str | None, until: float | None) -> None:
        """Mute; replaces the rule for the same camera and kind (a new end, or none)."""
        self.replace(lambda r: False, [MuteRule(camera, kind, until)])

    def replace(self, matches: Callable[[MuteRule], bool], add: list[MuteRule]) -> None:
        """Drop the rules ``matches`` says yes to and add ``add`` (each replacing the rule of its camera and kind), as one change."""
        now = time.time()
        if self._stopped:
            return
        rules = [r for r in self._rules if r.active(now) and not matches(r)]
        added = [r for r in add if r.until is None or now < r.until <= now + MUTE_MAX_SECONDS]
        rules = [r for r in rules if (r.camera, r.kind) not in {(a.camera, a.kind) for a in added}] + added
        # Bounded (a runaway script): the oldest timed rules go first, those
        # lasting until lifted (a switch turned on) only when there are no others;
        # never the ones just added.
        while len(rules) > MUTE_RULES_MAX:
            older = [r for r in rules if not any(r is a for a in added)] or rules
            rules.remove(next((r for r in older if r.until is not None), older[0]))
        self._rules = rules
        self._changed()

    def remove(self, matches: Callable[[MuteRule], bool]) -> int:
        """Unmute the rules ``matches`` says yes to; how many there were."""
        if self._stopped:
            return 0
        kept = [r for r in self._rules if not matches(r)]
        removed = len(self._rules) - len(kept)
        if removed:
            self._rules = kept
            self._changed()
        return removed

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Called whenever the rules change or one ends."""
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _changed(self) -> None:
        self._save()
        self._reschedule()
        for listener in list(self._listeners):
            listener()

    def _data(self) -> dict[str, Any]:
        return {"rules": [{"camera": r.camera, "kind": r.kind, "until": r.until} for r in self.rules()]}

    def _save(self) -> None:
        if self._stopped:
            return
        self._store.async_delay_save(self._data, 1)

    async def async_flush(self) -> None:
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()

    @callback
    def stop(self) -> None:
        self._stopped = True
        if self._timer is not None:
            self._timer()
            self._timer = None
        self._listeners.clear()

    def _reschedule(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None
        ends = [r.until for r in self._rules if r.until is not None]
        if ends:
            self._timer = async_track_point_in_utc_time(self.hass, self._ended, dt_util.utc_from_timestamp(min(ends)))

    @callback
    def _ended(self, _now: datetime) -> None:
        self._timer = None
        # A hair early rather than again at once: the timer's time is rounded.
        self._rules = [r for r in self._rules if r.active(time.time() + 0.001)]
        self._changed()
