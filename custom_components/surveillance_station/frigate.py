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
period), and again if something more important joins it later (person >
car > anything else > animals; never a kind that may be quiet: a person
after a dog, not a dog after a person, nor a quiet car after a dog),
for automations to notify with, carrying a signed image and, if
configured, a link to the card at the review's start. With Frigate's API
configured, the image is Frigate's snapshot of the review's foremost object
(box drawn) as it is when fetched: the object is in it whatever the delay
between the cameras' streams, and the event waits only
SNAPSHOT_SETTLE_SECONDS for a better frame than the review's first. Without
it (or when Frigate doesn't answer), the image is SS's frame of the moment
Frigate saw the object best, and the event waits for SS to have recorded that
moment (it lists recordings ~0-10 s behind), at most EVENT_WAIT_SECONDS. The
bookmark's thumbnail in the card is always SS's frame.

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
from dataclasses import dataclass, replace
from datetime import timedelta
import json
import logging
import math
import re
import time
from typing import Any

from aiohttp import web
from synology_ss_playback import Bookmark, Camera, SSAuthError, SSConnectionError, SSError, SurveillanceStationClient

from homeassistant.components import mqtt
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .bookmarks import name_kinds
from .const import (
    DETECTION_EVENT,
    DOMAIN,
    FRIGATE_ANIMALS,
    FRIGATE_ANNOUNCE_MAX_AGE,
    FRIGATE_BOOKMARK_LOOKBACK,
    FRIGATE_BOOKMARK_SLACK,
    FRIGATE_CAMERAS_TTL,
    FRIGATE_DECIDED_MAX,
    FRIGATE_EVENT_WAIT_SECONDS,
    FRIGATE_HEALTH_INTERVAL,
    FRIGATE_IMAGE_BACKOFF_SECONDS,
    FRIGATE_IMAGE_BUDGET_SECONDS,
    FRIGATE_IMAGE_FALLBACK_SECONDS,
    FRIGATE_IMAGE_URL,
    FRIGATE_ISSUE_AFTER_SECONDS,
    FRIGATE_MQTT_RETRY_SECONDS,
    FRIGATE_OPEN_BOOKMARK_SECONDS,
    FRIGATE_QUIET_KINDS,
    FRIGATE_RETRIES,
    FRIGATE_RETRY_SECONDS,
    FRIGATE_SNAPSHOT_SETTLE_SECONDS,
    FRIGATE_SNAPSHOT_TRIES,
    FRIGATE_THUMB_CACHE_BYTES,
    FRIGATE_THUMB_HEIGHT,
    FRIGATE_THUMB_PARALLEL,
    FRIGATE_TRACKED_MAX,
    LARGE_IMAGE_WIDTH,
)
from .frigate_api import FRIGATE_ID, FRIGATE_ID_PATTERN, FrigateAPI, FrigateAPIError
from .frigate_queue import Queued, ReviewQueue
from .manager import VodManager

_LOGGER = logging.getLogger(__name__)

DATA_FRIGATE: HassKey[dict[str, FrigateBridge]] = HassKey(f"{DOMAIN}_frigate")

# Patchable in tests (time.monotonic itself is the event loop's clock).
_monotonic = time.monotonic

# At the comment's end, as _find looks for it: nothing earlier in a comment
# (a severity off MQTT) can name another review.
_REVIEW_ID = re.compile(rf"\[frigate ({FRIGATE_ID_PATTERN})\]\Z")


def review_id_of(comment: str | None) -> str | None:
    """The Frigate review a bookmark was made for (its comment names it)."""
    return m.group(1) if (m := _REVIEW_ID.search(comment or "")) else None


def camera_key(name: str) -> str:
    """A camera name without case, spaces or punctuation (letters of any script kept)."""
    return re.sub(r"[\W_]", "", name.casefold())


def _rank(label: str) -> tuple[int, str]:
    # Named in a fixed order (Frigate's has none): people, vehicles, the rest, animals.
    order = {"person": 0, "car": 1}
    return (order.get(label, 3 if label in FRIGATE_ANIMALS else 2), label)


def kind_of(label: str) -> str:
    """A Frigate label as bookmarks and events name it: "Person", "Car", "Animal"."""
    return "Animal" if label in FRIGATE_ANIMALS else label.replace("_", " ").capitalize()


def kinds(objects: list[str]) -> list[str]:
    """What was seen, as bookmarks and events name it (each kind once)."""
    return list(dict.fromkeys(kind_of(o) for o in objects))


def bookmark_name(objects: list[str]) -> str:
    return ", ".join(kinds(objects)) or "Detection"


def bookmark_comment(review_id: str, severity: str) -> str:
    # No zones: only some cameras have them, and the camera already says where
    # (they are in the event). Frigate's severities only: the rest of the
    # message is whatever was published on the topic.
    if severity not in ("alert", "detection"):
        severity = "review"
    return f"Frigate {severity} [frigate {review_id}]"


@dataclass
class _Tracked:
    bookmark_id: int
    camera_id: int
    name: str
    comment: str
    start: int
    end: int
    frame: int | None = None


@dataclass(frozen=True)
class _Review:
    """What handle() reads of a review message."""

    kind: str  # new, update, end
    id: str
    after: dict[str, Any]
    camera: str  # Frigate's
    severity: str
    start: int
    ended: float | None  # its end, once it has one
    objects: list[str]  # the chosen ones seen, foremost first
    zones: list[str]
    # The frame Frigate picked as showing the object best (it may pick a
    # better one as the review goes on): the bookmark's thumbnail. SS cuts
    # from the keyframe at or before a second (every second here): rounded
    # up, that keyframe is within a second of Frigate's frame either way.
    frame: int | None


@dataclass(frozen=True)
class ReviewObjects:
    """A review's tracked objects as Frigate has them."""

    ended: bool
    ids: list[str]  # 16 at most
    found: list[Any]  # for each: its /api/events answer, or the exception instead


@dataclass(frozen=True)
class _Detection:
    """What a detection event says: the review, its bookmark and camera, what was seen."""

    review_id: str
    bookmark_id: int
    camera_id: int
    camera_name: str
    start: int
    after: dict[str, Any]
    objects: list[str]
    zones: list[str]
    frame: int


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
        api: FrigateAPI | None = None,
    ) -> None:
        self.hass = hass
        self.api = api
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
        # Reviews whose notification was decided: sent (the foremost object
        # it was sent with: something more important later is sent again),
        # or not (None: old news). Kept across restarts with _last_seen, so a
        # review that goes on over a restart is neither announced twice nor never.
        self._decided: OrderedDict[str, str | None] = OrderedDict()
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
        # Waiting for SS: the latest message of each review. Those that failed
        # because SS was unreachable are kept (across restarts too, for a day)
        # and tried again once it answers: an outage loses no bookmark.
        self._queue = ReviewQueue(self._selected)
        self._forget_bookmarks = False
        # SS refusing bookmarks (an error code) while answering otherwise
        # (rights taken away): since when, cleared by a bookmark made.
        self._rejected_since: float | None = None
        self._rejected: set[str] = set()  # the reviews refused since then
        self._worker: asyncio.Task | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        self._subscribed = False
        self._announcing: set[asyncio.Task] = set()
        self._announcing_ids: set[str] = set()
        # A message of a review being announced: the event says what it does.
        self._announcing_latest: dict[str, _Detection] = {}
        self._probe: asyncio.Task | None = None
        self._stop_health: Callable[[], None] | None = None
        self._stop_listener: Callable[[], None] | None = None
        self._started_at = _monotonic()
        self._mqtt_down_since: float | None = None  # subscribed, but HA's MQTT without its broker
        self._failing_since: float | None = None
        self._frigate_offline_since: float | None = None
        self._frigate_available: str | None = None
        self._issues: set[str] = set()
        self._dropping = False
        self._loading = False
        self._stopped = False
        # For diagnostics: is anything arriving, and where does it stop?
        # messages: every review message (new, each update, end), so several
        # per review. messages = handled + queued + coalesced + dropped
        # (+ those pushed out of a full queue to wait with the deferred);
        # handled ends as ignored (by reason), failed (after retries), or went
        # through (bookmarked counts new bookmarks; each review is then
        # announced or not_announced; announced_again counts its further
        # notifications, outside that sum). The queue counts coalesced and dropped.
        self._counts = {
            "messages": 0, "retried": 0, "failed": 0, "rejected": 0, "bookmarked": 0,
            "announced": 0, "announced_again": 0, "not_announced": 0, "held_quiet": 0, "announce_failed": 0,
            "images_frigate": 0, "images_ss": 0, "images_no_snapshot": 0, "images_failed": 0,
        }
        # Frigate's images: not asked again until then after a failure; its
        # last error (apart from the bookmarks' last_error).
        self._image_down_until = -math.inf
        self._image_error: tuple[float, str] | None = None
        self.thumb_sem = asyncio.Semaphore(FRIGATE_THUMB_PARALLEL)
        self._thumbs_down_until = -math.inf
        # Frigate's thumbnails of reviews that are over (they don't change
        # any more): (review id, height) -> (image, type), least recently used first.
        self._thumb_cache: OrderedDict[tuple[str, int | None], tuple[bytes, str]] = OrderedDict()
        self._thumb_cache_bytes = 0
        self._ss_image_error: tuple[float, str] | None = None  # SS's frame instead, failing too
        # Frigate's camera names (its config), for searching some cameras only.
        self._frigate_camera_names: list[str] = []
        self._frigate_cameras_at = -math.inf
        # Frigate's semantic search answers two queries at once with nothing
        # (0.18, measured: empty lists, no error): ours go one at a time.
        self.search_lock = asyncio.Lock()
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
            "frigate_api": self.api is not None,
            **self._counts,
            "coalesced": self._queue.coalesced,
            "dropped": self._queue.dropped,
            "ignored": dict(self._ignored),
            "queued": self._queue.queued,
            "deferred": self._queue.deferred,
            "announcing": len(self._announcing),
            "failing": self._failing,
            "issues": sorted(self._issues),
            "last_message": at(self._last_message),
            "last_error": self._last_error and {"at": at(self._last_error[0]), "error": self._last_error[1]},
            "last_image_error": self._image_error and {"at": at(self._image_error[0]), "error": self._image_error[1]},
            "last_ss_image_error": self._ss_image_error and {
                "at": at(self._ss_image_error[0]), "error": self._ss_image_error[1],
            },
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
        self._loading = True
        try:
            stored = await self._store.async_load() or {}
            now = time.time()
            for item in stored.get("decided") or []:
                # [id, foremost object sent or None]; before 0.18 just the id (not sent again).
                if isinstance(item, list) and len(item) == 2:
                    review_id, told = item
                else:
                    review_id, told = item, None
                if isinstance(review_id, (str, int, float)):
                    self._decide(str(review_id), told if isinstance(told, str) else None)
            for review_id, quiet in stored.get("not_yet") or []:
                self._note_not_yet(str(review_id), [str(k) for k in quiet])
            for camera_id, kind, t, *rid in stored.get("last_seen") or []:
                if now - float(t) < self.quiet:
                    self._last_seen[(int(camera_id), str(kind))] = (float(t), str(rid[0]) if rid else "")
            self._queue.load(stored.get("deferred") or [], now)
        except (ValueError, TypeError, AttributeError, HomeAssistantError):
            _LOGGER.warning("Ignoring unreadable Frigate state %s", store_key(self.entry_id))
        finally:
            self._loading = False
        if self._stopped:  # unloaded while its state was read: nothing to watch for
            return False
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
        self._queue.canary()  # left over from before a restart: try now, not in a minute
        return True

    @callback
    def stop(self) -> None:
        self._stopped = True
        self._subscribed = False
        # Not handled yet, or cut short: kept for next time (async_flush
        # writes it).
        self._queue.stop()
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
        # when the worker is cancelled (and a review cut short is the queue's current).
        self._save()

    async def async_flush(self) -> None:
        """Write what must survive a restart now (after stop(), on unload)."""
        if self._loading:
            # Stopped while reading it: nothing newer here than on disk (not
            # subscribed yet), and writing now would replace it with nothing.
            return
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
        item = Queued(review, received_at=time.time())
        self._last_message = time.time()
        key = str(review["after"].get("id") or "") or f"_{self._counts['messages']}"
        if self._queue.put(key, item) and not self._dropping:
            self._dropping = True
            _LOGGER.warning("Frigate reviews arrive faster than bookmarks can be made; the oldest wait for later")

    async def _work(self) -> None:
        # One review at a time, in order.
        while True:
            if self._failing and self._queue.hold_replay():
                self._save()
            if (next_ := self._queue.next()) is None:
                self._dropping = False
                if self._forget_bookmarks:  # once after a replay, not per bookmark
                    self._forget_bookmarks = False
                    self.manager.forget_bookmarks(self.entry_id)
                await self._queue.wait()
                continue
            # _process handles every Exception; cancelled (unload, HA
            # stopping), the message stays the queue's current.
            key, item = next_
            await self._process(key, item)
            self._queue.done(key)

    async def _process(self, key: str, item: Queued) -> None:
        retries = 0
        while True:
            try:
                await self.handle(item)
            except SSError as err:
                # A create may have gone through before the error (only the
                # answer lost): list the bookmarks afresh and look for it
                # before making one again. And the camera may have been
                # replaced in SS (same name, new id): list the cameras again.
                self.manager.forget_bookmarks(self.entry_id)
                self._cameras_at = -math.inf
                item = replace(item, maybe_made=True)
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
                    self._queue.defer(key, item)
                    self._save()
                    return
                retries += 1
                self._counts["retried"] += 1
                await asyncio.sleep(FRIGATE_RETRY_SECONDS)
                # A newer message of the same review may have come meanwhile.
                if (newer := self._queue.newer(key)) is not None:
                    item = replace(newer, maybe_made=True)
                self._queue.retrying(key, item)
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
        if self._queue.release():
            self._save()

    async def handle(self, message: dict[str, Any] | Queued) -> None:
        """Bookmark (and announce) a review message: queued, or a plain one, as just received."""
        item = message if isinstance(message, Queued) else Queued(message)
        if (review := self._parse(item.message)) is None:
            return
        tracked = self._tracked.get(review.id)
        if tracked is None and not review.objects:
            self._ignore("no_objects")  # none of the chosen objects (yet)
            if review.kind == "end":
                self._not_yet.pop(review.id, None)
            elif self._fresh(item):
                self._note_not_yet(review.id)
            return
        camera = await self.ss_camera(review.camera)
        if camera is None:
            self._ignore("unknown_camera")
            return
        camera_id, camera_name = camera
        name = bookmark_name(review.objects)
        comment = bookmark_comment(review.id, review.severity)
        # Still going on: up to now; a message replayed long after it came (SS
        # was down), whose end never came, up to when it came, not the replay
        # (received_at may be an earlier message's: how old the news is).
        until = time.time() if self._fresh(item) else item.came_at
        end = int(review.ended) + 1 if review.ended else max(review.start + FRIGATE_OPEN_BOOKMARK_SECONDS, int(until))
        if tracked is None and (item.maybe_made or not (review.kind == "new" or item.seen_new)):
            # Created before a restart, or by a try that failed after SS did
            # it (its comment names the review)?
            tracked = await self._find(review.id, camera_id)
        now = time.time()
        repeat = self._repeat(review, camera_id, now)
        tracked = await self._sync_bookmark(review, tracked, camera_id, name, comment, end)
        detection = _Detection(
            review.id, tracked.bookmark_id, camera_id, camera_name, review.start, review.after, review.objects,
            review.zones, review.frame or review.start,
        )
        self._decide_announcement(item, review, detection, repeat, now)
        self._mark_seen(item, review, camera_id, now)
        self._save()
        if review.frame is not None and review.frame != tracked.frame:
            tracked.frame = review.frame
            self.manager.set_frame(self.entry_id, tracked.bookmark_id, review.frame)
        if review.kind == "end":
            self._tracked.pop(review.id, None)

    def _parse(self, message: dict[str, Any]) -> _Review | None:
        """A review message as handle() reads it; None: not one (counted as ignored)."""
        kind = message.get("type")
        after = message["after"]
        review_id = str(after.get("id") or "")
        if kind not in ("new", "update", "end") or not FRIGATE_ID.fullmatch(review_id):
            self._ignore("malformed" if kind in ("new", "update", "end") else "other_type")
            return None
        try:
            start = int(float(after["start_time"]))
        except (KeyError, TypeError, ValueError):
            self._ignore("malformed")
            return None
        data = after.get("data") if isinstance(after.get("data"), dict) else {}
        # An object a sub label was given to (a known face, a plate) is
        # listed as "<label>-verified". Frigate lists them in no fixed order.
        seen = {str(o).removesuffix("-verified") for o in _strings(data.get("objects"))}
        try:
            ended = float(after["end_time"]) if after.get("end_time") else None
        except (TypeError, ValueError):
            ended = None
        try:
            frame = math.ceil(float(data["thumb_time"])) if data.get("thumb_time") else None
        except (TypeError, ValueError):
            frame = None
        return _Review(
            kind=kind,
            id=review_id,
            after=after,
            camera=str(after.get("camera") or ""),
            severity=str(after.get("severity")),
            start=start,
            ended=ended,
            objects=sorted((o for o in self.objects if o in seen), key=_rank),
            zones=_strings(data.get("zones")),
            frame=frame,
        )

    def _repeat(self, review: _Review, camera_id: int, now: float) -> bool:
        """Only kinds seen on this camera lately (before this review), and all of them ones
        that may be quiet (or kept quiet in this review): no second notification."""

        def seen_lately(kind: str) -> bool:
            t, by = self._last_seen.get((camera_id, kind), (-math.inf, ""))
            return by != review.id and now - t < self.quiet

        silenced = self._not_yet.get(review.id, frozenset())
        return bool(review.objects) and all(
            k in silenced or (k in self.quiet_kinds and seen_lately(k)) for k in kinds(review.objects)
        )

    async def _sync_bookmark(
        self, review: _Review, tracked: _Tracked | None, camera_id: int, name: str, comment: str, end: int
    ) -> _Tracked:
        """The review's bookmark: made, or changed as its name, comment or end did."""
        if tracked is None:
            bm = await self.client.create_bookmark(camera_id, name, review.start, end, comment)
            self._ok()
            tracked = _Tracked(bm.id, camera_id, name, comment, review.start, end)
            self._remember(review.id, tracked)
            self._counts["bookmarked"] += 1
            self._bookmarks_changed()
        elif (name, comment) != (tracked.name, tracked.comment) or (review.ended and end != tracked.end):
            try:
                await self.client.edit_bookmark(tracked.bookmark_id, camera_id, name, tracked.start, end, comment)
            except SSError as err:
                # Deleted in SS meanwhile, or an answer without the bookmark:
                # the next try looks for it afresh (and makes it again if it's
                # gone) rather than editing an id that may be dead at every
                # message. SS not reached, or "not now": the id still holds.
                if not _not_now(err):
                    self._tracked.pop(review.id, None)
                raise
            self._ok()
            tracked.name, tracked.comment, tracked.end = name, comment, end
            self._bookmarks_changed()
        return tracked

    def _decide_announcement(self, item: Queued, review: _Review, detection: _Detection, repeat: bool, now: float) -> None:
        review_id, objects = review.id, review.objects
        if review_id in self._announcing_ids:
            # Sent in a moment: with this message's objects (a person that
            # joined meanwhile is in it, rather than one more notification).
            if objects:
                self._announcing_latest[review_id] = detection
        elif review_id in self._decided:
            # Sent already: again if something more important has joined (a
            # person after a dog), of a kind that is never quiet (a quiet car
            # joining a dog's review is not worth a second alert).
            told = self._decided[review_id]
            if told is not None and self._fresh(item) and any(
                _rank(o)[0] < _rank(told)[0] and kind_of(o) not in self.quiet_kinds for o in objects
            ):
                self._announce(detection, again=True)
        else:
            # Once per review, as soon as it has its bookmark: normally at its
            # first message; later if that one failed (even at its end). Long
            # after it began (HA was down, SS unreachable) it is old news.
            # Decided for good only once the event has fired: one cut short
            # (unload, restart) is tried again by the review's next message.
            # News: a message received just now (not one replayed after an
            # outage) of a review that began lately, or that we saw going on
            # without being news until now (a person joining a quiet dog's
            # review, or appearing minutes into one of bicycles).
            fresh = self._fresh(item)
            news = objects and fresh and (now - review.start <= FRIGATE_ANNOUNCE_MAX_AGE or review_id in self._not_yet)
            if news and not repeat:
                self._announce(detection)
            elif news and review.kind != "end":
                # Only quiet kinds seen lately: not now, but a later message
                # adding another kind is news.
                if set(kinds(objects)) - self._not_yet.get(review_id, frozenset()):
                    self._counts["held_quiet"] += 1
                self._note_not_yet(review_id, kinds(objects))
            else:
                self._counts["not_announced"] += 1
                self._not_yet.pop(review_id, None)
                self._decide(review_id)

    def _mark_seen(self, item: Queued, review: _Review, camera_id: int, now: float) -> None:
        """Bookmarked: its kinds count as seen here (still going on: now; ended: when last
        active; one replayed long after: when it began, not now, or it would silence
        what happens now)."""
        active = review.ended or (now if self._fresh(item) else float(review.start))
        for k in kinds(review.objects):
            last = self._last_seen.get((camera_id, k))
            if last is None or active >= last[0]:
                self._last_seen[(camera_id, k)] = (active, review.id)
        if len(self._last_seen) > 256:
            self._last_seen = {k: v for k, v in self._last_seen.items() if now - v[0] < self.quiet}

    def _bookmarks_changed(self) -> None:
        # The card's list: re-read now, or once a replay is done (not for
        # each of hundreds of bookmarks it makes).
        if self._queue.replaying:
            self._forget_bookmarks = True
        else:
            self.manager.forget_bookmarks(self.entry_id)

    def _selected(self, message: dict[str, Any]) -> bool:
        """Does the message have any of the objects bookmarked?"""
        data = message["after"].get("data") if isinstance(message["after"].get("data"), dict) else {}
        return any(str(o).removesuffix("-verified") in self.objects for o in _strings(data.get("objects")))

    def _fresh(self, item: Queued) -> bool:
        """Received just now, not replayed after an outage (a message handled directly: now)."""
        return item.received_at is None or time.time() - item.received_at <= FRIGATE_ANNOUNCE_MAX_AGE

    def _note_not_yet(self, review_id: str, quiet: list[str] | None = None) -> None:
        self._not_yet[review_id] = self._not_yet.get(review_id, frozenset()) | frozenset(quiet or ())
        self._not_yet.move_to_end(review_id)
        while len(self._not_yet) > FRIGATE_TRACKED_MAX:
            self._not_yet.popitem(last=False)

    def _decide(self, review_id: str, told: str | None = None) -> None:
        self._decided[review_id] = told
        self._decided.move_to_end(review_id)
        while len(self._decided) > FRIGATE_DECIDED_MAX:
            self._decided.popitem(last=False)

    def _data(self) -> dict[str, Any]:
        return {
            "decided": [[rid, told] for rid, told in self._decided.items()],
            "last_seen": [[c, k, t, rid] for (c, k), (t, rid) in self._last_seen.items()],
            "not_yet": [[rid, sorted(quiet)] for rid, quiet in self._not_yet.items()],
            "deferred": self._queue.stored(),
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

    def _lookup(self, frigate_camera: str) -> tuple[int, str] | None:
        """The SS camera (id, name) a Frigate camera is, as SS last listed them."""
        key = camera_key(frigate_camera)
        if (name := self._aliases.get(key)) is not None:  # mapped in the options: that SS camera exactly
            return self._cameras_by_name.get(name)
        return self._cameras.get(key)

    async def ss_camera(self, frigate_camera: str) -> tuple[int, str] | None:
        """The SS camera (id, name) a Frigate camera is, as its reviews are bookmarked on."""
        age = _monotonic() - self._cameras_at
        # Listed again at most once a minute for a camera not there (added or
        # renamed), and every FRIGATE_CAMERAS_TTL anyway, or right after a
        # failure (replaced: same name, new id).
        if age > FRIGATE_CAMERAS_TTL or (self._lookup(frigate_camera) is None and age > 60):
            self._cameras_at = _monotonic()
            self._set_cameras(await self.client.cameras())
        found = self._lookup(frigate_camera)
        if found is None and frigate_camera not in self._unknown and len(self._unknown) < 64:
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

    def _payload(self, detection: _Detection) -> dict[str, Any]:
        d = detection
        return {
            "entry_id": self.entry_id,
            "review_id": d.review_id,
            "bookmark_id": d.bookmark_id,
            "camera_id": d.camera_id,
            "camera": d.camera_name,
            "frigate_camera": d.after.get("camera"),
            "severity": d.after.get("severity"),
            "objects": kinds(d.objects),
            "labels": list(dict.fromkeys(d.objects)),
            "zones": d.zones,
            "start": d.start,
            "image": self._image_url(d.review_id, d.camera_id, d.frame),
            "thumbnail": self.manager.sign_thumbnail(self.entry_id, d.camera_id, d.frame),
            "url": self._url(d.camera_id, d.start),
        }

    def _announce(self, detection: _Detection, again: bool = False) -> None:
        review_id, camera_id, frame = detection.review_id, detection.camera_id, detection.frame

        async def announce() -> None:
            # Not when cancelled (the entry unloads): the frame may not be there yet.
            try:
                if self._frigate_image(review_id):
                    await asyncio.sleep(FRIGATE_SNAPSHOT_SETTLE_SECONDS)
                else:
                    await wait_for_frame()
            finally:
                self._announcing_ids.discard(review_id)
                seen = self._announcing_latest.pop(review_id, detection)
            # Its bookmark and camera as announced; what was seen as its latest message says.
            latest = replace(detection, after=seen.after, objects=seen.objects, zones=seen.zones, frame=seen.frame)
            self.hass.bus.async_fire(DETECTION_EVENT, self._payload(latest))
            # A review counts as announced (or not) once; again: its later notifications.
            self._counts["announced_again" if again else "announced"] += 1
            self._not_yet.pop(review_id, None)
            self._decide(review_id, latest.objects[0])
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

        # Not yet news told until it has fired: cut short (unloaded while
        # waiting for the frame), it is news for the review's next message
        # after the reload, however long the review has gone on by then.
        if not again:  # a second one cut short: its told is unchanged, so it is tried again anyway
            self._note_not_yet(review_id)
        self._announcing_ids.add(review_id)
        task = self.hass.async_create_background_task(announce(), f"surveillance_station detection {review_id}")
        self._announcing.add(task)
        task.add_done_callback(self._announcing.discard)

    def _frigate_image(self, review_id: str) -> bool:
        return self.api is not None and FRIGATE_ID.fullmatch(review_id) is not None

    def _image_url(self, review_id: str, camera_id: int, frame: int) -> str:
        if self._frigate_image(review_id):
            # SS's frame (camera, moment) is in the path: the fallback.
            return self.manager.sign_path(f"{FRIGATE_IMAGE_URL}/{self.entry_id}/{camera_id}/{frame}/{review_id}.jpg")
        return self.manager.sign_thumbnail(self.entry_id, camera_id, frame, large=True)

    async def review_image(self, review_id: str, height: int | None = None) -> tuple[bytes, str] | None:
        """Frigate's snapshot (box drawn) of the review's foremost chosen object.

        Foremost: a person before a car before an animal, then the surest; the
        next one if Frigate has no snapshot of that. None: no such object with
        a snapshot (any more), or the review gone. FrigateAPIError: no answer
        from Frigate (unreachable, too slow, failing).
        """
        return (await self._review_snapshot(review_id, height))[0]

    async def review_objects(self, review_id: str) -> ReviewObjects | None:
        """A review's tracked objects as Frigate has them (None: the review gone, Frigate's
        retention). FrigateAPIError: anything else (refused: a wrong URL; not JSON; failing)
        is Frigate's trouble."""
        assert self.api is not None
        try:
            review = await self.api.json(f"/api/review/{review_id}")
        except FrigateAPIError as err:
            if err.status == 404:
                return None
            raise
        data = review.get("data") if isinstance(review, dict) else None
        ids = [i for i in _strings(data.get("detections") if isinstance(data, dict) else None) if FRIGATE_ID.fullmatch(i)]
        ids = ids[:16]
        found = await asyncio.gather(*(self.api.json(f"/api/events/{i}") for i in ids), return_exceptions=True)
        return ReviewObjects(isinstance(review, dict) and review.get("end_time") is not None, ids, found)

    async def _review_snapshot(self, review_id: str, height: int | None) -> tuple[tuple[bytes, str] | None, bool]:
        """review_image's answer, and whether the review is over (ended, or gone)."""
        if self.api is None:
            return None, False
        if (review := await self.review_objects(review_id)) is None:
            return None, True  # gone: no snapshot
        ids, found = review.ids, review.found
        # Not in Frigate's database yet: an object is written there a moment
        # after it's first seen (once it has a snapshot), while its snapshot
        # is served from memory already.
        unwritten = [i for i, f in zip(ids, found, strict=True) if isinstance(f, FrigateAPIError) and f.status == 404]
        if ids and all(isinstance(f, BaseException) for f in found) and not unwritten:
            raise next((f for f in found if isinstance(f, FrigateAPIError)), FrigateAPIError("/api/events: no answer"))
        ranked = [e["id"] for e in self.ranked([e for e in found if isinstance(e, dict) and e.get("has_snapshot")])]
        # A few at most: the phone is waiting (and running out the budget counts as Frigate failing).
        return await self._snapshot([*ranked, *unwritten], height), review.ended

    async def _snapshot(self, ids: list[str], height: int | None) -> tuple[bytes, str] | None:
        """The first of these objects' snapshots (box drawn) Frigate has; a few tried at most."""
        assert self.api is not None
        params: dict[str, Any] = {"bbox": 1, "quality": 90} | ({"height": height} if height else {})
        for i in ids[:FRIGATE_SNAPSHOT_TRIES]:
            try:
                return await self.api.image(f"/api/events/{i}/snapshot.jpg", params)
            except FrigateAPIError as err:
                # That object's snapshot gone, or not an image (HTTP 200): the next one's.
                if err.status not in (200, 404):
                    raise
        return None

    async def bookmark_objects(self, bm: Bookmark) -> list[dict[str, Any]]:
        """Frigate's objects (with a snapshot) in one of its bookmarks, by its camera and time and
        of its kinds, foremost first: how to find them once the review is gone (Frigate keeps
        reviews days, objects as long as their snapshots)."""
        assert self.api is not None
        params: dict[str, Any] = {
            "after": bm.start - FRIGATE_BOOKMARK_LOOKBACK, "before": bm.end + FRIGATE_BOOKMARK_SLACK, "has_snapshot": 1,
            "limit": 100,
        }
        if names := await self.frigate_cameras([bm.camera_id]):
            params["cameras"] = ",".join(names)
        near = await self.api.json("/api/events", params)
        # Of the bookmark's kinds: a parked car isn't what an Animal bookmark is of.
        its = {k.casefold() for k in name_kinds(bm.name)}
        mine = []
        for o in near if isinstance(near, list) else []:
            if not isinstance(o, dict) or not overlaps(o, bm):
                continue
            if kind_of(str(o.get("label") or "")).casefold() not in its:
                continue
            camera = o.get("camera")
            if isinstance(camera, str) and (found := await self.ss_camera(camera)) and found[0] == bm.camera_id:
                mine.append(o)
        return self.ranked(mine)

    def cached_thumbnail(self, review_id: str, height: int | None = None) -> tuple[bytes, str] | None:
        """bookmark_image's answer kept for a review that is over, if it is (no request to Frigate)."""
        key = (review_id, height)
        if (cached := self._thumb_cache.get(key)) is not None:
            self._thumb_cache.move_to_end(key)
        return cached

    async def bookmark_image(self, review_id: str, height: int | None = None) -> tuple[bytes, str] | None:
        """A Frigate bookmark's image: its review's snapshot, else (the review gone) that of
        the foremost object Frigate still has from the bookmark's camera and time."""
        # Kept meanwhile by another request for it (both waited for a permit).
        if (cached := self.cached_thumbnail(review_id, height)) is not None:
            return cached
        key = (review_id, height)
        found, over = await self._review_snapshot(review_id, height)
        if found is None:
            found = await self._bookmark_snapshot(review_id, height)
        if found is not None and over:
            # Asked again by every page of the event list, every device: once
            # the review is over its snapshot doesn't change.
            self._thumb_cache[key] = found
            self._thumb_cache_bytes += len(found[0])
            while self._thumb_cache_bytes > FRIGATE_THUMB_CACHE_BYTES:
                self._thumb_cache_bytes -= len(self._thumb_cache.popitem(last=False)[1][0])
        return found

    async def _bookmark_snapshot(self, review_id: str, height: int | None) -> tuple[bytes, str] | None:
        client = self.manager.client(self.entry_id)
        if client is None:
            return None
        try:
            marks = await self.manager.bookmarks(self.entry_id, client)
        except SSError:
            return None  # SS's trouble, not Frigate's: SS's frame (which may fail too)
        bm = next((b for b in marks if review_id_of(b.comment) == review_id), None)
        if bm is None:
            return None
        return await self._snapshot([o["id"] for o in await self.bookmark_objects(bm)], height)

    def bookmark_thumbnail(self, bm: Bookmark) -> str:
        """A bookmark's thumbnail URL (signed): Frigate's snapshot for one of its own, as the
        notification's image; SS's frame for any other (or with no Frigate URL)."""
        frame = self.manager.frame(self.entry_id, bm)
        rid = review_id_of(bm.comment)
        if self.api is None or not rid:
            return self.manager.sign_thumbnail(self.entry_id, bm.camera_id, frame)
        return self.manager.sign_path(f"{FRIGATE_IMAGE_URL}/{self.entry_id}/thumb/{bm.camera_id}/{frame}/{rid}.jpg")

    def ranked(self, events: list[Any]) -> list[dict[str, Any]]:
        """Of Frigate's tracked objects, the chosen ones, foremost first: a person, a car, an animal; then the surest."""
        chosen = [
            e for e in events
            if isinstance(e, dict) and isinstance(e.get("label"), str) and e["label"] in self.objects
            and isinstance(e.get("id"), str) and FRIGATE_ID.fullmatch(e["id"])
        ]
        return sorted(chosen, key=lambda e: (_rank(str(e["label"])), -_score(e)))

    def foremost(self, events: list[Any]) -> dict[str, Any] | None:
        """Of Frigate's tracked objects, the foremost chosen one (see ranked)."""
        return next(iter(self.ranked(events)), None)

    async def frigate_cameras(self, ss_ids: list[int]) -> list[str] | None:
        """Frigate's cameras that are these SS cameras (None: Frigate's list unknown)."""
        if self.api is None:
            return None
        wanted = set(ss_ids)

        async def names() -> list[str]:
            return [n for n in self._frigate_camera_names if (c := await self.ss_camera(n)) is not None and c[0] in wanted]

        age = _monotonic() - self._frigate_cameras_at
        found = await names() if age <= FRIGATE_CAMERAS_TTL else []
        # Listed again every FRIGATE_CAMERAS_TTL, or after a minute for a
        # camera not among them (added or renamed in Frigate).
        placed = {c[0] for n in found if (c := self._lookup(n)) is not None}
        if age > FRIGATE_CAMERAS_TTL or (len(placed & wanted) < len(wanted) and age > 60):
            self._frigate_cameras_at = _monotonic()  # also after a failure: not every search
            try:
                config = await self.api.json("/api/config")
            except FrigateAPIError:
                return None  # not known now: the caller filters Frigate's answer itself
            cameras = config.get("cameras") if isinstance(config, dict) else None
            if isinstance(cameras, dict):
                self._frigate_camera_names = [c for c in cameras if isinstance(c, str)]
            found = await names()
        if not self._frigate_camera_names:
            return None
        return found

    def image_from_frigate(self) -> bool:
        """Whether to ask Frigate for an image now (it has an API, and didn't fail just now)."""
        return self.api is not None and _monotonic() >= self._image_down_until

    def frigate_image_sent(self) -> None:
        """Frigate's image went to the phone."""
        self._counts["images_frigate"] += 1
        if self._image_down_until > -math.inf:
            _LOGGER.info("Frigate gives notification images again")
        self._image_down_until = -math.inf

    def frigate_image_missing(self, err: Exception | None = None) -> None:
        """Frigate gave no image: for want of a snapshot, or failing (err). SS's frame then: count_ss_image."""
        if err is None:
            self._counts["images_no_snapshot"] += 1
        else:
            self.frigate_failed(err)

    def thumbs_from_frigate(self) -> bool:
        """Whether to ask Frigate for bookmark thumbnails now (not while it, or they, just failed)."""
        return self.image_from_frigate() and _monotonic() >= self._thumbs_down_until

    def thumbs_failed(self) -> None:
        """A thumbnail from Frigate failed (unreachable, failing, slow): SS's frames for a minute,
        so a page of them doesn't each wait it out. Thumbnails only: a notification finds out
        for itself (a thumbnail's lookup is heavier, a slow one weak evidence)."""
        self._thumbs_down_until = _monotonic() + FRIGATE_IMAGE_BACKOFF_SECONDS

    def frigate_failed(self, err: Exception) -> None:
        """Frigate didn't give an image (unreachable, too slow, failing): shown, and not asked for a minute."""
        if isinstance(err, FrigateAPIError):
            text = str(err)
        else:
            detail = str(err)[:200]
            text = f"{type(err).__name__}: {detail}" if detail else type(err).__name__
        self._image_error = (time.time(), text)
        if self._image_down_until == -math.inf:
            _LOGGER.warning("No notification image from Frigate (%s); using Surveillance Station's frames for now", text)
        self._image_down_until = _monotonic() + FRIGATE_IMAGE_BACKOFF_SECONDS

    def count_ss_image(self, err: str | None = None) -> None:
        """How SS's frame (the fallback) went: sent, or not (err: why, and so no image at all)."""
        if err is None:
            self._counts["images_ss"] += 1
            return
        self._counts["images_failed"] += 1
        self._ss_image_error = (time.time(), err)

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

        if stale := self._queue.expire():
            _LOGGER.warning("Gave up on %d Frigate reviews Surveillance Station didn't take for a day", stale)
            self._save()
        if self._subscribed and self._queue.deferred:
            # The oldest again; if it goes through, _ok() brings the others.
            self._queue.canary()
        elif self._subscribed and self._failing and (self._probe is None or self._probe.done()):
            # Nothing waiting to go: is SS back at all?
            self._probe = self.hass.async_create_background_task(self._probe_ss(), "surveillance_station frigate probe")
        # Subscribed, but HA's MQTT has lost its broker since: nothing arrives either.
        if not self._subscribed or _mqtt_connected(self.hass):
            self._mqtt_down_since = None
        elif self._mqtt_down_since is None:
            self._mqtt_down_since = now
        problems = {
            "frigate_mqtt": (not self._subscribed and lasting(self._started_at)) or lasting(self._mqtt_down_since),
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


def _mqtt_connected(hass: HomeAssistant) -> bool:
    try:
        return mqtt.is_connected(hass)
    except KeyError:  # HA's MQTT not set up (removed, or reloading)
        return False


def event_time(value: Any) -> float | None:
    """A Frigate time (epoch seconds), if it is one."""
    try:
        t = float(value)
    except (TypeError, ValueError):
        return None
    return t if math.isfinite(t) and 0 < t < 2**32 else None


def overlaps(obj: dict[str, Any], bm: Bookmark) -> bool:
    """Whether a Frigate object was there during a bookmark (one still tracked: until now)."""
    start = event_time(obj.get("start_time"))
    if start is None:
        return False
    end = event_time(obj.get("end_time"))
    if end is None:
        end = time.time()  # still being tracked
    return start <= bm.end + FRIGATE_BOOKMARK_SLACK and end >= bm.start - FRIGATE_BOOKMARK_SLACK


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.frigate.{entry_id}"


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _score(event: dict[str, Any]) -> float:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    try:
        return float(data.get("top_score") or event.get("top_score") or 0)
    except (TypeError, ValueError):
        return 0.0


class FrigateImageView(HomeAssistantView):
    """A detection's notification image: Frigate's snapshot, else SS's frame.

    Signed like the thumbnails (see ThumbnailView): a phone fetches it with no
    credentials, and a bad signature is a 404, never a 401. Fetched when the
    notification arrives, so Frigate's snapshot is its best frame by then.
    """

    requires_auth = False
    url = FRIGATE_IMAGE_URL + r"/{entry_id}/{camera_id:\d+}/{frame:\d+}/{review_id:" + FRIGATE_ID_PATTERN + "}.jpg"
    name = "api:surveillance_station:frigate_image"

    def __init__(self, hass: HomeAssistant, manager: VodManager) -> None:
        self.hass = hass
        self.manager = manager

    async def get(
        self, request: web.Request, entry_id: str, camera_id: str, frame: str, review_id: str
    ) -> web.Response:
        if not self.manager.check_thumbnail(request.path, request.query.get("exp"), request.query.get("sig")):
            raise web.HTTPNotFound()
        bridge = self.hass.data.get(DATA_FRIGATE, {}).get(entry_id)
        if bridge is not None and bridge.image_from_frigate():
            failed: Exception | None = None
            try:
                # One budget for all of Frigate's requests: the phone is waiting.
                async with asyncio.timeout(FRIGATE_IMAGE_BUDGET_SECONDS):
                    found = await bridge.review_image(review_id)
            except Exception as err:  # noqa: BLE001 - whatever Frigate did, SS's frame instead
                _LOGGER.debug("Frigate snapshot for %s: %r", review_id, err)
                found, failed = None, err
            if found is not None:
                bridge.frigate_image_sent()
                body, content_type = found
                # It may get better while the review goes on; a phone fetches it once.
                return web.Response(body=body, content_type=content_type, headers={"Cache-Control": "private, max-age=60"})
            bridge.frigate_image_missing(failed)
        # No Frigate (unloaded, unreachable, failing) or no snapshot: SS's frame of the moment.
        # Counted (with why not) here: a notification without its image shows in the diagnostics.
        count = bridge.count_ss_image if bridge is not None and bridge.api is not None else lambda _err=None: None
        try:
            async with asyncio.timeout(FRIGATE_IMAGE_FALLBACK_SECONDS):
                jpg = await self.manager.thumbnail_when_recorded(
                    entry_id, int(camera_id), int(frame), FRIGATE_IMAGE_FALLBACK_SECONDS - 5, LARGE_IMAGE_WIDTH
                )
        except TimeoutError:
            count("no frame in time")
            raise web.HTTPGatewayTimeout() from None
        except SSError as err:
            _LOGGER.debug("Frame %s@%s failed: %s", camera_id, frame, err)
            count(str(err)[:200])
            raise web.HTTPBadGateway() from None
        except Exception as err:
            count(type(err).__name__)
            raise
        if jpg is None:
            count("not recorded in time (or SS not answering)")
            raise web.HTTPNotFound()
        count()
        return web.Response(body=jpg, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


class FrigateThumbnailView(HomeAssistantView):
    """A Frigate bookmark's thumbnail (event list, search results): Frigate's snapshot of its
    foremost object, box drawn, as the notification's image; else SS's frame (a redirect to
    its thumbnail). Signed like the rest."""

    requires_auth = False
    url = FRIGATE_IMAGE_URL + r"/{entry_id}/thumb/{camera_id:\d+}/{frame:\d+}/{review_id:" + FRIGATE_ID_PATTERN + "}.jpg"
    name = "api:surveillance_station:frigate_thumbnail"

    def __init__(self, hass: HomeAssistant, manager: VodManager) -> None:
        self.hass = hass
        self.manager = manager

    async def get(
        self, request: web.Request, entry_id: str, camera_id: str, frame: str, review_id: str
    ) -> web.Response:
        if not self.manager.check_thumbnail(request.path, request.query.get("exp"), request.query.get("sig")):
            raise web.HTTPNotFound()
        bridge = self.hass.data.get(DATA_FRIGATE, {}).get(entry_id)
        # Kept (its review over): asks nothing of Frigate, so neither waits out its backoff nor queues.
        found = None if bridge is None else bridge.cached_thumbnail(review_id, FRIGATE_THUMB_HEIGHT)
        if found is None and bridge is not None and bridge.thumbs_from_frigate():
            # Queued (a page asks for 30) outside the budget: waiting isn't Frigate being slow.
            async with bridge.thumb_sem:
                try:
                    if bridge.thumbs_from_frigate():  # not failed while this one waited
                        async with asyncio.timeout(FRIGATE_IMAGE_BUDGET_SECONDS):
                            found = await bridge.bookmark_image(review_id, FRIGATE_THUMB_HEIGHT)
                except (FrigateAPIError, TimeoutError) as err:
                    _LOGGER.debug("Frigate thumbnail for %s: %r", review_id, err)
                    bridge.thumbs_failed()
                except SSError as err:  # SS's camera list (to match Frigate's cameras): not Frigate's fault
                    _LOGGER.debug("Frigate thumbnail for %s: %r", review_id, err)
        if found is not None:
            body, content_type = found
            return web.Response(body=body, content_type=content_type, headers={"Cache-Control": "private, max-age=3600"})
        raise web.HTTPFound(self.manager.sign_thumbnail(entry_id, int(camera_id), int(frame)))


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


def _not_now(err: SSError) -> bool:
    """An error that says nothing of the bookmark asked for: SS unreachable, credentials refused,
    or one of DSM's "not now" codes (SS's own code, or an answer without one, may mean it is gone)."""
    return isinstance(err, (SSConnectionError, SSAuthError)) or (err.code is not None and _transient(err))
