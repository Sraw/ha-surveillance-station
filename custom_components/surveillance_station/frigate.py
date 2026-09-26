"""Frigate detections as Surveillance Station bookmarks.

Frigate publishes a review item on ``<topic>/reviews`` when something is seen
(``new``), as it changes (``update``: more objects, other zones, a detection
raised to an alert) and when it's over (``end``). Each review with an object
we keep (person, car, animal by default; any severity) becomes one SS
bookmark on the matching camera: created when it first qualifies, renamed as
objects are added, and given its real end at the end. Every animal is named
"Animal".
Frigate cameras are matched to SS cameras by name, ignoring case, spaces and
punctuation (``drive_way`` is "Drive Way"), or as the options map them.

A review is announced as a ``surveillance_station_detection`` event once it
has its bookmark (once per review, remembered across restarts; and not when the review has only kinds that may be quiet,
animals by default, never people, all seen on that camera within the quiet
period) for automations to notify with, carrying a signed frame of
the moment Frigate saw the object best and, if configured, a link to the card
at the review's start. The bookmark's thumbnail in the card is that frame too. The event waits for
SS to have recorded that moment (it lists recordings ~0-10 s behind), at most
EVENT_WAIT_SECONDS, so the frame is there when a phone fetches it.

The review id is kept in the bookmark's comment, so a review that ends after
a Home Assistant restart still finds its bookmark.

Reliability: a message that fails with a transient SS error is retried (a
review's newer message replacing it); messages waiting for SS are one per
review; the SS camera list is re-read regularly and after failures; MQTT is
waited for as long as it takes. A problem that lasts (no MQTT, bookmarks
failing, Frigate offline per ``<topic>/available``) becomes a Repairs issue,
removed once it clears.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import itertools
import json
import logging
import math
import re
import time
from typing import Any

from synology_ss_playback import Camera, SSAuthError, SSConnectionError, SSError, SurveillanceStationClient

from homeassistant.components import mqtt
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    DETECTION_EVENT,
    DOMAIN,
    FRIGATE_ANIMALS,
    FRIGATE_ANNOUNCE_MAX_AGE,
    FRIGATE_CAMERAS_TTL,
    FRIGATE_DECIDED_MAX,
    FRIGATE_DEFERRED_MAX_AGE,
    FRIGATE_EVENT_WAIT_SECONDS,
    FRIGATE_HEALTH_INTERVAL,
    FRIGATE_ISSUE_AFTER_SECONDS,
    FRIGATE_MQTT_RETRY_SECONDS,
    FRIGATE_QUEUE_MAX,
    FRIGATE_RETRIES,
    FRIGATE_RETRY_SECONDS,
    FRIGATE_OPEN_BOOKMARK_SECONDS,
    FRIGATE_QUIET_KINDS,
    FRIGATE_TRACKED_MAX,
    LARGE_IMAGE_WIDTH,
)
from .views import VodManager

_LOGGER = logging.getLogger(__name__)

DATA_FRIGATE: HassKey[dict[str, FrigateBridge]] = HassKey(f"{DOMAIN}_frigate")

# Patchable in tests (time.monotonic itself is the event loop's clock).
_monotonic = time.monotonic

_REVIEW_ID = re.compile(r"\[frigate ([A-Za-z0-9._-]{1,64})\]")


def camera_key(name: str) -> str:
    """A camera name without case, spaces or punctuation (letters of any script kept)."""
    return re.sub(r"[\W_]", "", name.casefold())


def _rank(label: str) -> tuple[int, str]:
    # Named in a fixed order (Frigate's has none): people, vehicles, the rest, animals.
    order = {"person": 0, "car": 1}
    return (order.get(label, 3 if label in FRIGATE_ANIMALS else 2), label)


def kinds(objects: list[str]) -> list[str]:
    """What was seen, as bookmarks and events name it: "Person", "Car", "Animal"."""
    return list(dict.fromkeys("Animal" if o in FRIGATE_ANIMALS else o.replace("_", " ").capitalize() for o in objects))


def bookmark_name(objects: list[str]) -> str:
    return ", ".join(kinds(objects)) or "Detection"


def bookmark_comment(review_id: str, severity: str, zones: list[str]) -> str:
    where = f" in {', '.join(z.replace('_', ' ') for z in zones)}" if zones else ""
    return f"Frigate {severity}{where} [frigate {review_id}]"


@dataclass
class _Tracked:
    bookmark_id: int
    camera_id: int
    name: str
    comment: str
    start: int
    end: int
    frame: int | None = None


class FrigateBridge:
    """One config entry's Frigate review subscription."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client: SurveillanceStationClient,
        manager: VodManager,
        topic: str,
        objects: set[str],
        link: str,
        quiet_minutes: float = 0,
        quiet_kinds: set[str] | None = None,
        cameras: dict[str, str] | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.client = client
        self.manager = manager
        self.topic = topic.strip("/")
        self.objects = objects
        self.link = link
        self.quiet = quiet_minutes * 60
        # Frigate camera (as camera_key) -> SS camera (its exact name), where the names differ.
        self._aliases = {camera_key(f): ss for ss, frigate in (cameras or {}).items() for f in frigate_names(frigate)}
        # Never "Person", whatever is passed.
        self.quiet_kinds = (quiet_kinds or set()) & set(FRIGATE_QUIET_KINDS)
        # (SS camera id, kind) -> when that kind was last active on that
        # camera, and in which review (a review never silences itself).
        self._last_seen: dict[tuple[int, str], tuple[float, str]] = {}
        # Reviews whose notification was decided (sent, or not: quiet, old
        # news). Kept across restarts with _last_seen, so a review that goes
        # on over a restart is neither announced twice nor never.
        self._decided: OrderedDict[str, None] = OrderedDict()
        # Reviews seen live but not announceable yet (none of the chosen
        # objects yet, or only quiet kinds): a later message that makes them
        # news is announced then, however long ago they began. With the
        # kinds kept quiet in them: those stay quiet for that review.
        self._not_yet: OrderedDict[str, frozenset[str]] = OrderedDict()
        self._store: Store[dict[str, Any]] = Store(hass, 1, store_key(entry_id))
        self._cameras: dict[str, tuple[int, str]] = {}  # camera_key -> (SS id, SS name)
        self._cameras_by_name: dict[str, tuple[int, str]] = {}  # SS name -> (SS id, SS name)
        self._cameras_at = -math.inf
        self._unknown: set[str] = set()  # Frigate cameras warned about
        self._tracked: OrderedDict[str, _Tracked] = OrderedDict()
        # Waiting for SS: the latest message of each review, in the order the
        # reviews first came in. A review's later message replaces its earlier
        # one (it says everything the earlier one did), so a backlog after an
        # outage is one message per review, not a minute-old queue of updates.
        self._pending: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Reviews that failed because SS was unreachable: kept (across
        # restarts too, for a day) and tried again once it answers. The
        # oldest is tried every minute; once one goes through the rest are
        # replayed, after anything fresh, and back to waiting if SS fails
        # again meanwhile. An outage loses no bookmark.
        self._deferred: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._replay: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._current: tuple[str, dict[str, Any]] | None = None  # being handled
        self._canary_key: str | None = None
        self._forget_bookmarks = False
        # SS refusing bookmarks (an error code) while answering otherwise
        # (rights taken away): since when, cleared by a bookmark made.
        self._rejected_since: float | None = None
        self._rejected: set[str] = set()  # the reviews refused since then
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        self._subscribed = False
        self._announcing: set[asyncio.Task] = set()
        self._announcing_ids: set[str] = set()
        self._probe: asyncio.Task | None = None
        self._stop_health: Callable[[], None] | None = None
        self._stop_listener: Callable[[], None] | None = None
        self._started_at = _monotonic()
        self._failing_since: float | None = None
        self._frigate_offline_since: float | None = None
        self._frigate_available: str | None = None
        self._issues: set[str] = set()
        self._dropping = False
        self._stopped = False
        # For diagnostics: is anything arriving, and where does it stop?
        # messages: every review message (new, each update, end), so several
        # per review. messages = handled + queued + coalesced + dropped
        # (+ those pushed out of a full queue to wait with the deferred);
        # handled ends as ignored (by reason), failed (after retries), or went
        # through (bookmarked counts new bookmarks; each review is then
        # announced or not_announced).
        self._counts = {
            "messages": 0, "coalesced": 0, "dropped": 0, "retried": 0, "failed": 0, "rejected": 0, "bookmarked": 0,
            "announced": 0, "not_announced": 0, "held_quiet": 0, "announce_failed": 0,
        }
        self._ignored: dict[str, int] = {}
        self._last_message: float | None = None
        self._last_error: tuple[float, str] | None = None
        self._bookmark_error = "-"  # the last SS error making a bookmark, for the Repairs issue

    @property
    def _failing(self) -> bool:
        return self._failing_since is not None

    def stats(self) -> dict[str, Any]:
        """Diagnostics: subscription, counters since start, the last error."""

        def at(ts: float | None) -> str | None:
            return None if ts is None else dt_util.utc_from_timestamp(ts).isoformat()

        return {
            "subscribed": self._subscribed,
            "topic": f"{self.topic}/reviews",
            "frigate_available": self._frigate_available,
            "objects": sorted(self.objects, key=_rank),
            "quiet_minutes": self.quiet / 60,
            "quiet_kinds": sorted(self.quiet_kinds),
            **self._counts,
            "ignored": dict(self._ignored),
            "queued": len(self._pending) + len(self._replay),
            "deferred": len(self._deferred),
            "announcing": len(self._announcing),
            "failing": self._failing,
            "issues": sorted(self._issues),
            "last_message": at(self._last_message),
            "last_error": self._last_error and {"at": at(self._last_error[0]), "error": self._last_error[1]},
        }

    def _ignore(self, reason: str) -> None:
        self._ignored[reason] = self._ignored.get(reason, 0) + 1

    def _error(self, err: Exception) -> None:
        # Diagnostics end up in bug reports: an SSError's text is ours (no
        # URL, no sid); anything else may quote a payload, so keep it short.
        text = str(err) if isinstance(err, SSError) else f"{type(err).__name__}: {err}"
        self._last_error = (time.time(), text[:300])

    async def start(self) -> bool:
        """Subscribe once MQTT is there (False: stopped first); stop() undoes it."""
        try:
            stored = await self._store.async_load() or {}
            now = time.time()
            for review_id in stored.get("decided") or []:
                self._decide(str(review_id))
            for review_id, quiet in stored.get("not_yet") or []:
                self._note_not_yet(str(review_id), [str(k) for k in quiet])
            for camera_id, kind, t, *rid in stored.get("last_seen") or []:
                if now - float(t) < self.quiet:
                    self._last_seen[(int(camera_id), str(kind))] = (float(t), str(rid[0]) if rid else "")
            for key, review in stored.get("deferred") or []:
                if isinstance(review, dict) and isinstance(review.get("after"), dict):
                    review.setdefault(_FAILED_AT, now)
                    # Kept by a delayed save: it may have been bookmarked
                    # after it (HA died before the next), so look first.
                    review[_MAYBE_MADE] = True
                    self._deferred[str(key)] = review
        except (ValueError, TypeError, AttributeError, HomeAssistantError):
            _LOGGER.warning("Ignoring unreadable Frigate state %s", store_key(self.entry_id))
        self._stop_health = async_track_time_interval(
            self.hass, self._check_health, timedelta(seconds=FRIGATE_HEALTH_INTERVAL)
        )
        # HA stopping doesn't unload entries (stop() isn't called): what
        # waits is written at its final write instead.
        self._stop_listener = self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._ha_stopping)
        warned = False
        # HA's MQTT may be starting, retrying its broker, or reloading. Keep
        # waiting (this runs as an entry task, cancelled on unload) rather
        # than give up for good.
        while True:
            unsubscribe: list[Callable[[], None]] = []
            if await mqtt.async_wait_for_mqtt_client(self.hass):
                try:
                    unsubscribe.append(await mqtt.async_subscribe(self.hass, f"{self.topic}/reviews", self._received))
                    unsubscribe.append(
                        await mqtt.async_subscribe(self.hass, f"{self.topic}/available", self._availability)
                    )
                    break
                except HomeAssistantError:  # MQTT went away in between
                    for u in unsubscribe:
                        u()
            if self._stopped:
                return False
            if not warned:
                warned = True
                _LOGGER.warning("MQTT is not available yet; Frigate detections are bookmarked once it is")
            await asyncio.sleep(FRIGATE_MQTT_RETRY_SECONDS)
        if self._stopped:  # unloaded while MQTT was starting
            for u in unsubscribe:
                u()
            return False
        if warned:
            _LOGGER.info("MQTT is available: bookmarking Frigate detections")
        self._unsubscribe = unsubscribe
        self._subscribed = True
        self._worker = self.hass.async_create_background_task(self._work(), "surveillance_station frigate")
        self._canary()  # left over from before a restart: try now, not in a minute
        return True

    @callback
    def stop(self) -> None:
        self._stopped = True
        self._subscribed = False
        # Not handled yet, or cut short: kept for next time (async_flush
        # writes it).
        self._deferred = self._waiting()
        self._replay.clear()
        self._pending.clear()
        self._current = None
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        for remove in (self._stop_health, self._stop_listener):
            if remove is not None:
                remove()
        self._stop_health = self._stop_listener = None
        for task in (self._worker, self._probe, *self._announcing):
            if task is not None:
                task.cancel()
        # An unloaded entry's problems are not problems any more (a reload
        # checks again from scratch).
        for issue in list(self._issues):
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(issue))
        self._issues.clear()

    @callback
    def _ha_stopping(self, _event: Event) -> None:
        # A delayed save while stopping is written at HA's final write, by
        # when the worker is cancelled (and a review cut short is _current).
        self._save()

    async def async_flush(self) -> None:
        """Write what must survive a restart now (after stop(), on unload)."""
        await self._store.async_save(self._data())

    @callback
    def _availability(self, msg: mqtt.ReceiveMessage) -> None:
        if self._stopped:
            return
        state = str(msg.payload).strip().lower()
        self._frigate_available = state
        if state == "online":
            self._frigate_offline_since = None
        elif self._frigate_offline_since is None:
            self._frigate_offline_since = _monotonic()

    @callback
    def _received(self, msg: mqtt.ReceiveMessage) -> None:
        # After stop(): HA's MQTT restores subscriptions onto a new client
        # when it reloads, and an unsubscribe made before that may not reach it.
        if self._stopped:
            return
        try:
            review = json.loads(msg.payload)
        except (ValueError, RecursionError):
            return
        if not (isinstance(review, dict) and isinstance(review.get("after"), dict)):
            return
        self._counts["messages"] += 1
        review[_RECEIVED_AT] = time.time()
        self._last_message = time.time()
        key = str(review["after"].get("id") or "") or f"_{self._counts['messages']}"
        # A review's newer message replaces what of it waits anywhere,
        # keeping what the earlier one told: it was new; a try of it failed
        # (so its bookmark may exist).
        earlier = self._pending.get(key) or self._replay.pop(key, None) or self._deferred.pop(key, None)
        if earlier is not None:
            self._counts["coalesced"] += 1
            if earlier.get("type") == "new" or earlier.get(_SEEN_NEW):
                review[_SEEN_NEW] = True
            if earlier.get(_MAYBE_MADE):
                review[_MAYBE_MADE] = True
            # News since the earlier one came (it had an object of interest,
            # waited for SS): as old as that, or a replay would seem fresh.
            if earlier.get(_RECEIVED_AT) and self._selected(earlier):
                review[_RECEIVED_AT] = min(review[_RECEIVED_AT], earlier[_RECEIVED_AT])
        self._pending[key] = review  # an existing key keeps its place
        if len(self._pending) > FRIGATE_QUEUE_MAX:
            # SS stuck for a long while: the oldest (but not the one being
            # tried to see whether SS is back) waits with those SS failed.
            oldest = next(k for k in itertools.islice(self._pending, 2) if k != self._canary_key)
            self._keep(oldest, self._pending.pop(oldest))
            if not self._dropping:
                self._dropping = True
                _LOGGER.warning("Frigate reviews arrive faster than bookmarks can be made; the oldest wait for later")
        self._wake.set()

    async def _work(self) -> None:
        # One review at a time, in order.
        while True:
            if self._failing and self._replay:
                # SS failed again mid-replay: the rest wait again (no request
                # each, stalling fresh reviews behind timeouts).
                for key, review in self._replay.items():
                    self._deferred.setdefault(key, review)
                self._replay.clear()
                self._save()
            if self._pending:  # fresh reviews first
                key, review = self._pending.popitem(last=False)
            elif self._replay:
                key, review = self._replay.popitem(last=False)
            else:
                self._dropping = False
                if self._forget_bookmarks:  # once after a replay, not per bookmark
                    self._forget_bookmarks = False
                    self.manager.forget_bookmarks(self.entry_id)
                self._wake.clear()
                await self._wake.wait()
                continue
            # Kept if cancelled (unload, HA stopping): stop() and the final
            # write keep it for next time. _process handles every Exception.
            self._current = (key, review)
            await self._process(key, review)
            self._current = None
            if key == self._canary_key:
                self._canary_key = None

    async def _process(self, key: str, review: dict[str, Any]) -> None:
        retries = 0
        while True:
            try:
                await self.handle(review)
            except SSError as err:
                # A create may have gone through before the error (only the
                # answer lost): list the bookmarks afresh and look for it
                # before making one again. And the camera may have been
                # replaced in SS (same name, new id): list the cameras again.
                self.manager.forget_bookmarks(self.entry_id)
                self._cameras_at = -math.inf
                review = {**review, _MAYBE_MADE: True}
                transient = _transient(err)
                if not transient:
                    # SS said no to this one (an error code): trying it again
                    # every minute would only say no again. Not "failing"
                    # (SS answers), but a Repairs issue if it lasts.
                    self._counts["rejected"] += 1
                    self._error(err)
                    self._bookmark_error = str(err)[:300]
                    if self._rejected_since is None:
                        self._rejected_since = _monotonic()
                    if len(self._rejected) < 16:
                        self._rejected.add(key)
                    _LOGGER.warning("Surveillance Station refused the bookmark of Frigate review %s: %s", key, err)
                    return
                if self._failing or retries >= FRIGATE_RETRIES:
                    self._fail(err)
                    self._defer(key, review)
                    return
                retries += 1
                self._counts["retried"] += 1
                await asyncio.sleep(FRIGATE_RETRY_SECONDS)
                # A newer message of the same review may have come meanwhile.
                if (newer := self._pending.pop(key, None)) is not None:
                    review = {**newer, _MAYBE_MADE: True}
                if self._current is not None and self._current[0] == key:
                    self._current = (key, review)
                continue
            except Exception as err:  # noqa: BLE001 - one bad message must not stop the bridge
                # This message, not SS: counted and logged, not retried, and
                # not "failing" (that is about SS).
                self._counts["failed"] += 1
                self._error(err)
                _LOGGER.warning("Frigate review %s not handled", key, exc_info=True)
            return

    def _fail(self, err: SSError) -> None:
        self._counts["failed"] += 1
        self._error(err)
        self._bookmark_error = str(err)[:300]
        self.manager.track(self.entry_id, err)
        if not self._failing:
            self._failing_since = _monotonic()
            _LOGGER.warning("Frigate review not turned into a bookmark (tried again once SS answers): %s", err)

    def _defer(self, key: str, review: dict[str, Any]) -> None:
        if not self._newer_waits(key):
            self._keep(key, review)
        self._save()

    def _newer_waits(self, key: str, queues: tuple[OrderedDict[str, dict[str, Any]], ...] | None = None) -> bool:
        """Is a newer message of the review in flight waiting? It then goes instead, knowing the bookmark may exist.

        Anything of the review waiting while one of its messages is handled
        came later: _received takes an earlier one out of every queue."""
        for queue in queues or (self._pending, self._replay, self._deferred):
            if (newer := queue.get(key)) is not None:
                newer[_MAYBE_MADE] = True
                return True
        return False

    def _waiting(self) -> OrderedDict[str, dict[str, Any]]:
        """Everything not bookmarked yet, one message per review, as kept for next time (queues untouched)."""
        waiting = OrderedDict((k, dict(r)) for k, r in self._deferred.items())
        replay = OrderedDict((k, dict(r)) for k, r in self._replay.items())
        pending = OrderedDict((k, dict(r)) for k, r in self._pending.items())
        if self._current is not None:
            key, review = self._current
            if not self._newer_waits(key, (pending, replay, waiting)):
                replay[key] = {**review, _MAYBE_MADE: True}
        for key, review in (*replay.items(), *pending.items()):
            self._keep(key, review, waiting)
        return waiting

    def _keep(
        self, key: str, review: dict[str, Any], into: OrderedDict[str, dict[str, Any]] | None = None
    ) -> None:
        """Wait for SS (the review's latest message, with what earlier ones told)."""
        into = self._deferred if into is None else into
        earlier = into.pop(key, None) or {}
        review = {**review, _FAILED_AT: earlier.get(_FAILED_AT) or review.get(_FAILED_AT) or time.time()}
        for flag in (_MAYBE_MADE, _SEEN_NEW):
            if earlier.get(flag):
                review[flag] = True
        into[key] = _compact(review)
        if len(into) > FRIGATE_QUEUE_MAX:
            into.popitem(last=False)
            if into is self._deferred:
                self._counts["dropped"] += 1

    def _canary(self) -> None:
        """Try the oldest review waiting for SS again, first in line."""
        if self._deferred:
            key, review = self._deferred.popitem(last=False)
            self._pending.setdefault(key, review)
            self._pending.move_to_end(key, last=False)
            self._canary_key = key
            self._wake.set()

    def _ok(self, bookmark: bool = True) -> None:
        """A bookmark was made or changed: not failing, and what failed meanwhile is tried again.

        Or found: SS answering reads says nothing about bookmarks (a camera
        removed, rights taken away), and requeueing on reads could go round
        in circles."""
        if self._failing:
            self._failing_since = None
            _LOGGER.info("Surveillance Station answers again: bookmarking Frigate detections")
        if bookmark:
            self._rejected_since = None
            self._rejected.clear()
        if self._deferred:
            for key, review in self._deferred.items():
                if key not in self._pending:  # a newer message waiting wins
                    self._replay.setdefault(key, review)
            self._deferred.clear()
            self._wake.set()
            self._save()

    async def handle(self, review: dict[str, Any]) -> None:
        kind = review.get("type")
        after = review["after"]
        review_id = str(after.get("id") or "")
        if kind not in ("new", "update", "end") or not _REVIEW_ID.fullmatch(f"[frigate {review_id}]"):
            self._ignore("malformed" if kind in ("new", "update", "end") else "other_type")
            return
        try:
            start = int(float(after["start_time"]))
        except (KeyError, TypeError, ValueError):
            self._ignore("malformed")
            return
        data = after.get("data") if isinstance(after.get("data"), dict) else {}
        # An object a sub label was given to (a known face, a plate) is
        # listed as "<label>-verified". Frigate lists them in no fixed order.
        seen = {str(o).removesuffix("-verified") for o in _strings(data.get("objects"))}
        objects = sorted((o for o in self.objects if o in seen), key=_rank)
        tracked = self._tracked.get(review_id)
        if tracked is None and not objects:
            self._ignore("no_objects")  # none of the chosen objects (yet)
            if kind == "end":
                self._not_yet.pop(review_id, None)
            elif self._live(review):
                self._note_not_yet(review_id)
            return
        camera = await self._camera(str(after.get("camera") or ""))
        if camera is None:
            self._ignore("unknown_camera")
            return
        camera_id, camera_name = camera
        zones = _strings(data.get("zones"))
        name = bookmark_name(objects)
        comment = bookmark_comment(review_id, str(after.get("severity")), zones)
        try:
            ended = float(after["end_time"]) if after.get("end_time") else None
        except (TypeError, ValueError):
            ended = None
        end = int(ended) + 1 if ended else max(start + FRIGATE_OPEN_BOOKMARK_SECONDS, int(time.time()))

        if tracked is None and (review.get(_MAYBE_MADE) or not (kind == "new" or review.get(_SEEN_NEW))):
            # Created before a restart, or by a try that failed after SS did
            # it (its comment names the review)?
            tracked = await self._find(review_id, camera_id)
        # The frame Frigate picked as showing the object best (it may pick a
        # better one as the review goes on): the bookmark's thumbnail. SS cuts
        # from the keyframe at or before a second (every second here): rounded
        # up, that keyframe is within a second of Frigate's frame either way.
        try:
            frame = math.ceil(float(data["thumb_time"])) if data.get("thumb_time") else None
        except (TypeError, ValueError):
            frame = None
        # Only kinds seen on this camera lately (before this review), and all
        # of them ones that may be quiet: no second notification.
        now = time.time()
        def seen_lately(kind: str) -> bool:
            t, by = self._last_seen.get((camera_id, kind), (-math.inf, ""))
            return by != review_id and now - t < self.quiet

        silenced = self._not_yet.get(review_id, frozenset())
        repeat = bool(objects) and all(
            k in silenced or (k in self.quiet_kinds and seen_lately(k)) for k in kinds(objects)
        )
        if tracked is None:
            bm = await self.client.create_bookmark(camera_id, name, start, end, comment)
            self._ok()
            tracked = _Tracked(bm.id, camera_id, name, comment, start, end)
            self._remember(review_id, tracked)
            self._counts["bookmarked"] += 1
            self._bookmarks_changed()
        elif (name, comment) != (tracked.name, tracked.comment) or (ended and end != tracked.end):
            await self.client.edit_bookmark(tracked.bookmark_id, camera_id, name, tracked.start, end, comment)
            self._ok()
            tracked.name, tracked.comment, tracked.end = name, comment, end
            self._bookmarks_changed()
        if review_id not in self._decided and review_id not in self._announcing_ids:
            # Once per review, as soon as it has its bookmark: normally at its
            # first message; later if that one failed (even at its end). Long
            # after it began (HA was down, SS unreachable) it is old news.
            # Decided for good only once the event has fired: one cut short
            # (unload, restart) is tried again by the review's next message.
            # News: a message received just now (not one replayed after an
            # outage) of a review that began lately, or that we saw going on
            # without being news until now (a person joining a quiet dog's
            # review, or appearing minutes into one of bicycles).
            live = self._live(review)
            news = objects and live and (now - start <= FRIGATE_ANNOUNCE_MAX_AGE or review_id in self._not_yet)
            if news and not repeat:
                self._not_yet.pop(review_id, None)
                self._announce(
                    review_id, tracked.bookmark_id, camera_id, camera_name, after, objects, zones, start, frame or start
                )
            elif news and kind != "end":
                # Only quiet kinds seen lately: not now, but a later message
                # adding another kind is news.
                if set(kinds(objects)) - self._not_yet.get(review_id, frozenset()):
                    self._counts["held_quiet"] += 1
                self._note_not_yet(review_id, kinds(objects))
            else:
                self._counts["not_announced"] += 1
                self._not_yet.pop(review_id, None)
                self._decide(review_id)
        # Bookmarked: its kinds count as seen here (still going on: now;
        # ended: when last active; one replayed long after: when it began,
        # not now, or it would silence what happens now).
        active = ended or (now if self._live(review) else float(start))
        for k in kinds(objects):
            last = self._last_seen.get((camera_id, k))
            if last is None or active >= last[0]:
                self._last_seen[(camera_id, k)] = (active, review_id)
        if len(self._last_seen) > 256:
            self._last_seen = {k: v for k, v in self._last_seen.items() if now - v[0] < self.quiet}
        self._save()
        if frame is not None and frame != tracked.frame:
            tracked.frame = frame
            self.manager.set_frame(self.entry_id, tracked.bookmark_id, frame)
        if kind == "end":
            self._tracked.pop(review_id, None)

    def _bookmarks_changed(self) -> None:
        # The card's list: re-read now, or once a replay is done (not for
        # each of hundreds of bookmarks it makes).
        if self._replay:
            self._forget_bookmarks = True
        else:
            self.manager.forget_bookmarks(self.entry_id)

    def _selected(self, review: dict[str, Any]) -> bool:
        """Does the message have any of the objects bookmarked?"""
        data = review["after"].get("data") if isinstance(review["after"].get("data"), dict) else {}
        return any(str(o).removesuffix("-verified") in self.objects for o in _strings(data.get("objects")))

    def _live(self, review: dict[str, Any]) -> bool:
        """Received just now, not replayed after an outage (a message handled directly: now)."""
        return time.time() - review.get(_RECEIVED_AT, time.time()) <= FRIGATE_ANNOUNCE_MAX_AGE

    def _note_not_yet(self, review_id: str, quiet: list[str] | None = None) -> None:
        self._not_yet[review_id] = self._not_yet.get(review_id, frozenset()) | frozenset(quiet or ())
        self._not_yet.move_to_end(review_id)
        while len(self._not_yet) > FRIGATE_TRACKED_MAX:
            self._not_yet.popitem(last=False)

    def _decide(self, review_id: str) -> None:
        self._decided[review_id] = None
        while len(self._decided) > FRIGATE_DECIDED_MAX:
            self._decided.popitem(last=False)

    def _data(self) -> dict[str, Any]:
        return {
            "decided": list(self._decided),
            "last_seen": [[c, k, t, rid] for (c, k), (t, rid) in self._last_seen.items()],
            "not_yet": [[rid, sorted(quiet)] for rid, quiet in self._not_yet.items()],
            "deferred": [[key, review] for key, review in self._waiting().items()],
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 2)

    def _remember(self, review_id: str, tracked: _Tracked) -> None:
        self._tracked[review_id] = tracked
        while len(self._tracked) > FRIGATE_TRACKED_MAX:
            self._tracked.popitem(last=False)

    async def _find(self, review_id: str, camera_id: int) -> _Tracked | None:
        tag = f"[frigate {review_id}]"
        for bm in await self.manager.bookmarks(self.entry_id, self.client):
            if bm.camera_id == camera_id and bm.comment.endswith(tag):
                tracked = _Tracked(bm.id, camera_id, bm.name, bm.comment, bm.start, bm.end)
                self._remember(review_id, tracked)
                self._ok(bookmark=False)  # SS answers; made before, so not a refusal cleared
                return tracked
        return None

    async def _camera(self, frigate_camera: str) -> tuple[int, str] | None:
        key = camera_key(frigate_camera)

        def lookup() -> tuple[int, str] | None:
            if (name := self._aliases.get(key)) is not None:  # mapped in the options: that SS camera exactly
                return self._cameras_by_name.get(name)
            return self._cameras.get(key)

        age = _monotonic() - self._cameras_at
        # Listed again at most once a minute for a camera not there (added or
        # renamed), and every FRIGATE_CAMERAS_TTL anyway, or right after a
        # failure (replaced: same name, new id).
        if age > FRIGATE_CAMERAS_TTL or (lookup() is None and age > 60):
            self._cameras_at = _monotonic()
            self._set_cameras(await self.client.cameras())
        if (found := lookup()) is None and frigate_camera not in self._unknown and len(self._unknown) < 64:
            self._unknown.add(frigate_camera)
            _LOGGER.warning(
                "Frigate camera %r matches no Surveillance Station camera by name (map it in the integration's"
                " options); its detections are not bookmarked",
                frigate_camera,
            )
        return found

    def _set_cameras(self, cameras: list[Camera]) -> None:
        self._cameras = {camera_key(c.name): (c.id, c.name) for c in cameras}
        self._cameras_by_name = {c.name: (c.id, c.name) for c in cameras}

    def _announce(
        self,
        review_id: str,
        bookmark_id: int,
        camera_id: int,
        camera_name: str,
        after: dict[str, Any],
        objects: list[str],
        zones: list[str],
        start: int,
        frame: int,
    ) -> None:
        payload = {
            "entry_id": self.entry_id,
            "review_id": review_id,
            "bookmark_id": bookmark_id,
            "camera_id": camera_id,
            "camera": camera_name,
            "frigate_camera": after.get("camera"),
            "severity": after.get("severity"),
            "objects": kinds(objects),
            "labels": list(dict.fromkeys(objects)),
            "zones": zones,
            "start": start,
            "image": self.manager.sign_thumbnail(self.entry_id, camera_id, frame, large=True),
            "thumbnail": self.manager.sign_thumbnail(self.entry_id, camera_id, frame),
            "url": self._url(camera_id, start),
        }

        async def announce() -> None:
            # Not when cancelled (the entry unloads): the frame may not be there yet.
            try:
                await wait_for_frame()
            finally:
                self._announcing_ids.discard(review_id)
            self.hass.bus.async_fire(DETECTION_EVENT, payload)
            self._counts["announced"] += 1
            self._decide(review_id)
            self._save()

        async def wait_for_frame() -> None:
            try:
                # Bounded: a slow SS or a busy ffmpeg must not hold the
                # notification (the phone fetches the frame on demand anyway).
                async with asyncio.timeout(FRIGATE_EVENT_WAIT_SECONDS + 10):
                    await self.manager.thumbnail_when_recorded(
                        self.entry_id, camera_id, frame, FRIGATE_EVENT_WAIT_SECONDS, LARGE_IMAGE_WIDTH
                    )
            except Exception as err:  # noqa: BLE001 - announce without the frame ready
                self._counts["announce_failed"] += 1
                self._error(err)
                _LOGGER.warning("Frame for detection %s not ready: %r", review_id, err)

        self._announcing_ids.add(review_id)
        task = self.hass.async_create_background_task(announce(), f"surveillance_station detection {review_id}")
        self._announcing.add(task)
        task.add_done_callback(self._announcing.discard)

    def _url(self, camera_id: int, start: int) -> str | None:
        if not self.link:
            return None
        sep = "&" if "?" in self.link else "?"
        # A few seconds before: the card's own event jumps start there too.
        return f"{self.link}{sep}ss_camera={camera_id}&ss_time={start - 3}"

    def _issue_id(self, issue: str) -> str:
        return f"{issue}_{self.entry_id}"

    @callback
    def _check_health(self, _now: Any = None) -> None:
        """Problems that last become Repairs issues, and go when they do."""
        now = _monotonic()

        def lasting(since: float | None) -> bool:
            return since is not None and now - since >= FRIGATE_ISSUE_AFTER_SECONDS

        stale = [k for k, r in self._deferred.items() if time.time() - r.get(_FAILED_AT, 0) > FRIGATE_DEFERRED_MAX_AGE]
        for key in stale:
            del self._deferred[key]
            self._counts["dropped"] += 1
        if stale:
            _LOGGER.warning("Gave up on %d Frigate reviews Surveillance Station didn't take for a day", len(stale))
            self._save()
        if self._subscribed and self._deferred:
            # The oldest again; if it goes through, _ok() brings the others.
            self._canary()
        elif self._subscribed and self._failing and (self._probe is None or self._probe.done()):
            # Nothing waiting to go: is SS back at all?
            self._probe = self.hass.async_create_background_task(self._probe_ss(), "surveillance_station frigate probe")
        problems = {
            "frigate_mqtt": not self._subscribed and lasting(self._started_at),
            # Refusals: of more than one review (a single review refused,
            # nothing after it, is not "bookmarks failing").
            "frigate_failing": lasting(self._failing_since)
            or (lasting(self._rejected_since) and len(self._rejected) > 1),
            "frigate_offline": lasting(self._frigate_offline_since),
        }
        for issue, present in problems.items():
            if present and issue not in self._issues:
                self._issues.add(issue)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    self._issue_id(issue),
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=issue,
                    translation_placeholders={
                        "topic": self.topic,
                        "error": self._bookmark_error,
                    },
                )
            elif not present and issue in self._issues:
                self._issues.discard(issue)
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(issue))


    async def _probe_ss(self) -> None:
        """Bookmarks failed and nothing is waiting: does SS answer again?"""
        try:
            cameras = await self.client.cameras()
        except SSError as err:
            self._bookmark_error = str(err)[:300]
        else:
            self._set_cameras(cameras)
            self._cameras_at = _monotonic()
            self._ok(bookmark=False)  # SS answers; says nothing about refusals
        finally:
            self._probe = None


def frigate_names(value: str) -> list[str]:
    """The Frigate camera names an option lists (comma-separated)."""
    return [n.strip() for n in str(value or "").split(",") if n.strip()]


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.frigate.{entry_id}"


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


# Set on a queued message that replaced its review's "new": it was seen new.
_SEEN_NEW = "_seen_new"
# Set on a message whose try failed: its bookmark may exist all the same.
_MAYBE_MADE = "_maybe_made"
_FAILED_AT = "_failed_at"  # when it first failed (epoch), for giving up after a day
_RECEIVED_AT = "_received_at"  # when the message came (epoch): a replay of an old one is no news


_TRANSIENT_CODES = frozenset({None, 100, 102, 103, 104, 106, 107, 119, 407})


def _transient(err: SSError) -> bool:
    """Worth trying again later: SS unreachable, overloaded or blocking us for
    now; credentials refused (a reauth fixes that); or one of DSM's common
    codes for "not now": unknown error, API or method not there (the SS
    package stopped or updating), session gone. Not 101/114 (bad
    parameters), nor 105 (no permission, still after the client logged in
    again): those, and SS's own codes (400 and up) for this request, would
    only come again."""
    code = err.code
    return isinstance(err, (SSConnectionError, SSAuthError)) or code in _TRANSIENT_CODES


def _compact(review: dict[str, Any]) -> dict[str, Any]:
    """What handle() reads of a message, and our flags: kept, possibly on disk."""
    after = review["after"]
    data = after.get("data") if isinstance(after.get("data"), dict) else {}
    return {
        **{k: v for k, v in review.items() if k.startswith("_")},
        "type": review.get("type"),
        "after": {
            **{k: after.get(k) for k in ("id", "camera", "start_time", "end_time", "severity")},
            "data": {k: data.get(k) for k in ("objects", "zones", "thumb_time")},
        },
    }
