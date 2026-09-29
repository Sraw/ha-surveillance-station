"""Muting notifications: rules saying which detections are announced as muted.

A rule is a camera (or every camera), a kind (or every kind) and an end (or
none: until it is lifted). A detection is muted when each kind in it is covered
by some rule for its camera: a dog and a person at once, with only animals
muted, is not. Muting affects the notification only: the detection is still
bookmarked, and its event is still fired, with ``muted: true`` for an automation
to respect (the shipped blueprint does).

The cameras are named by their Surveillance Station id (a rename changes
nothing), the kinds as the event names them (Person, Car, Animal, ...).
Rules stored by 0.22 name their camera by ``camera_key`` (a name without case,
spaces or punctuation); they wait for SS's list of cameras, which says which
camera each was, and are dropped if it names none.
A rule for every kind may leave some kinds out (one turned off on a muted
camera), so that the camera's other kinds, any label at all, stay muted.
Rules are kept across restarts and dropped when they end.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
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
    camera: int | None  # SS camera id; None: every camera
    kind: str | None  # None: every kind
    until: float | None  # epoch seconds; None: until it is lifted
    # Of every kind, the kinds left out (only with kind None).
    excluded: frozenset[str] = frozenset()

    def covers(self, camera: int | None, kind: str) -> bool:
        """Mutes kind on camera (None: on every camera, which only a rule for every camera does)."""
        return self.camera in (None, camera) and (kind not in self.excluded if self.kind is None else self.kind == kind)

    def active(self, now: float) -> bool:
        return self.until is None or self.until > now


def _acceptable(rule: MuteRule, now: float) -> bool:
    """Still in force, and not beyond MUTE_MAX_SECONDS (nor infinite, nor NaN)."""
    return rule.until is None or now < rule.until <= now + MUTE_MAX_SECONDS


def outlasts(end: float | None, other: float | None) -> bool:
    """Whether a mute ending at end lasts longer than one ending at other (None: until lifted)."""
    return other is not None and (end is None or end > other)


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.mute.{entry_id}"


# Version 2 names cameras by id ("camera_id"); 0.22's version 1 by key ("camera"), which
# is what a rule still waiting for its camera is stored as. The data is read by the shape
# of each rule, so version 1 needs no conversion, and 0.22 refuses version 2 rather than
# reading "camera_id" rules as rules for every camera.
# A rule of 0.22 whose camera no listing has had for this long is dropped.
WAITING_GONE_AFTER = 1800
STORE_VERSION = 2


class _Migrate(Store[dict[str, Any]]):
    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]) -> dict[str, Any]:
        if old_major_version == 1:
            return old_data
        raise NotImplementedError


@dataclass
class _Waiting:
    """A rule of 0.22, its camera named by key, until the SS cameras are listed."""

    camera: str
    kind: str | None
    until: float | None
    excluded: frozenset[str] = frozenset()
    missed: float | None = None  # since when the listings have had no camera with this key


def _stored(rule: MuteRule | _Waiting) -> dict[str, Any]:
    # Without "excluded" when there are none: as 0.22.2 wrote them.
    data: dict[str, Any] = {"kind": rule.kind, "until": rule.until}
    if isinstance(rule, _Waiting):
        data["camera"] = rule.camera
    else:
        data["camera_id"] = rule.camera
    if rule.excluded:
        data["excluded"] = sorted(rule.excluded)
    return data


class MuteRules:
    """One config entry's mute rules."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._store: Store[dict[str, Any]] = _Migrate(hass, STORE_VERSION, store_key(entry_id))
        self._rules: list[MuteRule] = []
        self._waiting: list[_Waiting] = []
        self._listeners: list[Callable[[], None]] = []
        self._timer: CALLBACK_TYPE | None = None
        self._stopped = False

    async def async_load(self) -> None:
        try:
            stored = await self._store.async_load() or {}
            now = time.time()
            seen: set[tuple[int | str | None, str | None]] = set()
            for item in stored.get("rules") or []:
                excluded = item.get("excluded") or []
                if not isinstance(excluded, list):
                    raise TypeError(excluded)
                kind = None if item.get("kind") is None else str(item["kind"])
                until = None if item.get("until") is None else float(item["until"])
                left_out = frozenset(str(k) for k in excluded) if kind is None else frozenset()
                if "camera_id" in item:
                    camera: int | str | None = None if item["camera_id"] is None else int(item["camera_id"])
                else:  # 0.22's: a camera key, or none for every camera
                    camera = None if item.get("camera") is None else str(item["camera"])
                rule = MuteRule(camera, kind, until, left_out) if not isinstance(camera, str) else _Waiting(camera, kind, until, left_out)
                if _acceptable(rule, now) and (rule.camera, rule.kind) not in seen:
                    seen.add((rule.camera, rule.kind))
                    (self._waiting if isinstance(rule, _Waiting) else self._rules).append(rule)
                    if len(self._rules) + len(self._waiting) == MUTE_RULES_MAX:
                        break
        except (ValueError, TypeError, AttributeError, HomeAssistantError):
            _LOGGER.warning("Ignoring unreadable mute rules %s", store_key(self.entry_id))
            self._rules, self._waiting = [], []
        self._reschedule()

    @callback
    def async_name_cameras(self, ids_by_key: dict[str, int]) -> None:
        """The SS cameras have been listed (their ids by key): the rules of 0.22 become theirs.

        A rule whose key no camera has waits on (SS may list only some of its
        cameras now and then), and is dropped once that has lasted
        WAITING_GONE_AFTER seconds, over two listings at least (the camera was
        removed or renamed while its rule waited). An empty listing (SS
        answering oddly) changes nothing.
        """
        if not self._waiting or not ids_by_key:
            return
        waiting, self._waiting = self._waiting, []
        now = time.time()
        rules = list(self._rules)
        for w in waiting:
            if (camera := ids_by_key.get(w.camera)) is None:
                if w.missed is None:
                    w.missed = now
                if now - w.missed < WAITING_GONE_AFTER:
                    self._waiting.append(w)
                else:
                    _LOGGER.warning("Dropping the mute rule for %r: no Surveillance Station camera has that name any more", w.camera)
            elif not any((r.camera, r.kind) == (camera, w.kind) for r in rules) and _acceptable(rule := MuteRule(camera, w.kind, w.until, w.excluded), now):
                rules.append(rule)
        self._rules = rules[-MUTE_RULES_MAX:]
        self._changed()

    def rules(self, now: float | None = None) -> list[MuteRule]:
        """The rules in force (ended ones are dropped by the timer, but not relied on)."""
        now = time.time() if now is None else now
        return [r for r in self._rules if r.active(now)]

    def is_muted(self, camera: int, kinds: list[str], now: float | None = None) -> bool:
        if not kinds:  # kinds unknown: only when every kind is
            return self.coverage(camera, None, now)[0]
        rules = self.rules(now)
        return all(any(r.covers(camera, k) for r in rules) for k in kinds)

    def coverage(self, camera: int | None, kind: str | None, now: float | None = None) -> tuple[bool, float | None]:
        """Whether every detection of kind on camera is muted (None: every camera, every kind), and until when.

        What is_muted says of each such detection. Every camera (SS may list
        more later) is muted only by rules for every camera; every kind (a
        label may be anything) only by rules for every kind, with the kinds
        they leave out muted by rules of their own. Rules only end, so the
        mute lasts until the first end after which it no longer holds (None:
        until lifted).
        """
        now = time.time() if now is None else now
        rules = [r for r in self.rules(now) if r.camera in (None, camera)]

        def holds(rules: list[MuteRule]) -> bool:
            if kind is not None:
                return any(r.covers(camera, kind) for r in rules)
            left_out = {k for r in rules for k in r.excluded}
            return any(r.kind is None for r in rules) and all(any(r.covers(camera, k) for r in rules) for k in left_out)

        if not holds(rules):
            return False, None
        for end in sorted({r.until for r in rules if r.until is not None}):
            if not holds([r for r in rules if r.active(end)]):
                return True, end
        return True, None

    def add(self, camera: int | None, kind: str | None, until: float | None) -> None:
        """Mute; replaces the rule for the same camera and kind (a new end, or none)."""
        self.replace(lambda r: False, [MuteRule(camera, kind, until)])

    def extend(self, camera: int | None, until: float | None) -> None:
        """Mute every kind on camera, never shortening a mute of it.

        Its rule keeps the longer end (None: until lifted); what that rule
        leaves out is muted until then by rules of its own (again never
        shortening one).
        """
        held = {(r.camera, r.kind): r for r in self.rules()}

        def longer(kind: str | None) -> bool:
            return (old := held.get((camera, kind))) is None or outlasts(until, old.until)

        if longer(None):
            self.add(camera, None, until)
        else:
            self.replace(lambda r: False, [MuteRule(camera, k, until) for k in held[(camera, None)].excluded if longer(k)])

    def replace(self, matches: Callable[[MuteRule], bool], new: list[MuteRule]) -> None:
        """Drop the rules ``matches`` says yes to and add ``new`` (each replacing the rule of its camera and kind), as one change."""
        now = time.time()
        if self._stopped:
            return
        added = [r for r in new if _acceptable(r, now)]
        replaced = {(a.camera, a.kind) for a in added}
        rules = [r for r in self._rules if r.active(now) and not matches(r) and (r.camera, r.kind) not in replaced] + added
        # Bounded (a runaway script): the oldest timed rules go first, those
        # lasting until lifted (a switch turned on) only when there are no others;
        # never the ones just added.
        while len(rules) > MUTE_RULES_MAX:
            older = [r for r in rules if not any(r is a for a in added)] or rules
            rules.remove(next((r for r in older if r.until is not None), older[0]))
        # The same rules in another order (one replaced by an equal one goes last) are no change.
        if Counter(rules) == Counter(self._rules):
            return
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
        now = time.time()
        return {"rules": [_stored(r) for r in (*self.rules(), *(w for w in self._waiting if _acceptable(w, now)))]}

    def _save(self) -> None:
        if self._stopped:
            return
        self._store.async_delay_save(self._data, 1)

    async def async_flush(self) -> None:
        await self._store.async_save(self._data())

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
