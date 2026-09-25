"""Frigate detections as Surveillance Station bookmarks.

Frigate publishes a review item on ``<topic>/reviews`` when something is seen
(``new``), as it changes (``update``: more objects, other zones, a detection
raised to an alert) and when it's over (``end``). Each review with an object
we keep (person, car, animal by default; any severity) becomes one SS
bookmark on the matching camera: created when it first qualifies, renamed as
objects are added, and given its real end at the end. Every animal is named
"Animal".
Frigate cameras are matched to SS cameras by name, ignoring case, spaces and
punctuation (``drive_way`` is "Drive Way").

A created bookmark is announced as a ``surveillance_station_detection`` event
(once per review, and not when the camera saw only the same kinds within the
quiet period) for automations to notify with, carrying a signed frame of
the moment Frigate saw the object best and, if configured, a link to the card
at the review's start. The bookmark's thumbnail in the card is that frame too. The event waits for
SS to have recorded that moment (it lists recordings ~0-10 s behind), at most
EVENT_WAIT_SECONDS, so the frame is there when a phone fetches it.

The review id is kept in the bookmark's comment, so a review that ends after
a Home Assistant restart still finds its bookmark.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import math
import re
import time
from typing import Any

from synology_ss_playback import SSError, SurveillanceStationClient

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .const import (
    DETECTION_EVENT,
    DOMAIN,
    FRIGATE_ANIMALS,
    FRIGATE_ANNOUNCE_MAX_AGE,
    FRIGATE_QUEUE_MAX,
    FRIGATE_EVENT_WAIT_SECONDS,
    FRIGATE_OPEN_BOOKMARK_SECONDS,
    FRIGATE_TRACKED_MAX,
    LARGE_IMAGE_WIDTH,
)
from .views import VodManager

_LOGGER = logging.getLogger(__name__)

DATA_FRIGATE: HassKey[dict[str, FrigateBridge]] = HassKey(f"{DOMAIN}_frigate")

_REVIEW_ID = re.compile(r"\[frigate ([A-Za-z0-9._-]{1,64})\]")


def camera_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


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
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.client = client
        self.manager = manager
        self.topic = topic.strip("/")
        self.objects = objects
        self.link = link
        self.quiet = quiet_minutes * 60
        # (SS camera id, kind) -> when that kind was last active on that camera.
        self._last_seen: dict[tuple[int, str], float] = {}
        self._cameras: dict[str, tuple[int, str]] = {}  # camera_key -> (SS id, SS name)
        self._cameras_at = 0.0
        self._unknown: set[str] = set()  # Frigate cameras warned about
        self._tracked: OrderedDict[str, _Tracked] = OrderedDict()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(FRIGATE_QUEUE_MAX)
        self._worker: asyncio.Task | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._announcing: set[asyncio.Task] = set()
        self._failing = False
        self._dropping = False
        self._stopped = False

    async def start(self) -> bool:
        """Subscribe (False: MQTT isn't available); stop() undoes it."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            _LOGGER.error("MQTT is not available: Frigate detections are not bookmarked (reload the entry once it is)")
            return False
        unsubscribe = await mqtt.async_subscribe(self.hass, f"{self.topic}/reviews", self._received)
        if self._stopped:  # unloaded while MQTT was starting
            unsubscribe()
            return False
        self._unsubscribe = unsubscribe
        self._worker = self.hass.async_create_background_task(self._work(), "surveillance_station frigate")
        return True

    @callback
    def stop(self) -> None:
        self._stopped = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for task in (self._worker, *self._announcing):
            if task is not None:
                task.cancel()

    @callback
    def _received(self, msg: mqtt.ReceiveMessage) -> None:
        try:
            review = json.loads(msg.payload)
        except (ValueError, RecursionError):
            return
        if not (isinstance(review, dict) and isinstance(review.get("after"), dict)):
            return
        try:
            self._queue.put_nowait(review)
        except asyncio.QueueFull:
            # SS stuck for a long while: newer reviews matter more.
            self._queue.get_nowait()
            self._queue.put_nowait(review)
            if not self._dropping:
                self._dropping = True
                _LOGGER.warning("Frigate reviews arrive faster than bookmarks can be made; dropping the oldest")

    async def _work(self) -> None:
        # One at a time and in order: an alert can end before its bookmark
        # has been created.
        while True:
            review = await self._queue.get()
            try:
                await self.handle(review)
            except SSError as err:
                # A create may have gone through before the error: list afresh
                # before looking for it again.
                self.manager.forget_bookmarks(self.entry_id)
                self._fail(err)
            except Exception as err:  # noqa: BLE001 - one bad message must not stop the bridge
                self._fail(err)
            else:
                if self._failing:
                    self._failing = False
                    _LOGGER.info("Frigate bookmarks work again")
            if self._queue.empty():
                self._dropping = False

    def _fail(self, err: Exception) -> None:
        if isinstance(err, SSError):
            self.manager.track(self.entry_id, err)
        if not self._failing:
            self._failing = True
            _LOGGER.warning("Frigate review not turned into a bookmark: %r", err, exc_info=not isinstance(err, SSError))

    async def handle(self, review: dict[str, Any]) -> None:
        kind = review.get("type")
        after = review["after"]
        review_id = str(after.get("id") or "")
        if kind not in ("new", "update", "end") or not _REVIEW_ID.fullmatch(f"[frigate {review_id}]"):
            return
        try:
            start = int(float(after["start_time"]))
        except (KeyError, TypeError, ValueError):
            return
        data = after.get("data") or {}
        # An object a sub label was given to (a known face, a plate) is
        # listed as "<label>-verified". Frigate lists them in no fixed order.
        seen = {str(o).removesuffix("-verified") for o in data.get("objects") or []}
        objects = sorted((o for o in self.objects if o in seen), key=_rank)
        tracked = self._tracked.get(review_id)
        if tracked is None and not objects:
            return
        camera = await self._camera(str(after.get("camera") or ""))
        if camera is None:
            return
        camera_id, camera_name = camera
        zones = [str(z) for z in data.get("zones") or []]
        name = bookmark_name(objects)
        comment = bookmark_comment(review_id, str(after.get("severity")), zones)
        ended = after.get("end_time")
        end = int(float(ended)) + 1 if ended else max(start + FRIGATE_OPEN_BOOKMARK_SECONDS, int(time.time()))

        if tracked is None and kind != "new":
            # Created before a restart (its comment names the review)?
            tracked = await self._find(review_id, camera_id)
        # The frame Frigate picked as showing the object best (it may pick a
        # better one as the review goes on): the bookmark's thumbnail. SS cuts
        # from the keyframe at or before a second (every second here): rounded
        # up, that keyframe is within a second of Frigate's frame either way.
        frame = math.ceil(float(data["thumb_time"])) if data.get("thumb_time") else None
        # Seen on this camera lately (before this review): no second notification.
        now = time.time()
        repeat = bool(objects) and all(
            now - self._last_seen.get((camera_id, k), -math.inf) < self.quiet for k in kinds(objects)
        )
        if tracked is None:
            bm = await self.client.create_bookmark(camera_id, name, start, end, comment)
            tracked = _Tracked(bm.id, camera_id, name, comment, start, end)
            self._remember(review_id, tracked)
            self.manager.forget_bookmarks(self.entry_id)
            # One only heard of at its end (Home Assistant was down), or long
            # after it began (SS was unreachable, a restart mid-review), gets
            # its bookmark, but is old news for a notification.
            if kind != "end" and now - start <= FRIGATE_ANNOUNCE_MAX_AGE and not repeat:
                self._announce(review_id, bm.id, camera_id, camera_name, after, objects, zones, start, frame or start)
        elif (name, comment) != (tracked.name, tracked.comment) or (ended and end != tracked.end):
            await self.client.edit_bookmark(tracked.bookmark_id, camera_id, name, tracked.start, end, comment)
            tracked.name, tracked.comment, tracked.end = name, comment, end
            self.manager.forget_bookmarks(self.entry_id)
        # Bookmarked: its kinds count as seen here (still going on: now;
        # ended: when last active).
        active = float(ended) if ended else now
        for k in kinds(objects):
            self._last_seen[(camera_id, k)] = max(self._last_seen.get((camera_id, k), 0), active)
        if len(self._last_seen) > 256:
            self._last_seen = {k: v for k, v in self._last_seen.items() if now - v < self.quiet}
        if frame is not None and frame != tracked.frame:
            tracked.frame = frame
            self.manager.set_frame(self.entry_id, tracked.bookmark_id, frame)
        if kind == "end":
            self._tracked.pop(review_id, None)

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
                return tracked
        return None

    async def _camera(self, frigate_camera: str) -> tuple[int, str] | None:
        key = camera_key(frigate_camera)
        if key not in self._cameras and time.monotonic() - self._cameras_at > 60:
            # Listed again at most once a minute, for a camera added or renamed.
            self._cameras_at = time.monotonic()
            self._cameras = {camera_key(c.name): (c.id, c.name) for c in await self.client.cameras()}
        if (found := self._cameras.get(key)) is None and frigate_camera not in self._unknown and len(self._unknown) < 64:
            self._unknown.add(frigate_camera)
            _LOGGER.warning(
                "Frigate camera %r matches no Surveillance Station camera by name; its detections are not bookmarked",
                frigate_camera,
            )
        return found

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
            await self.manager.thumbnail_when_recorded(
                self.entry_id, camera_id, frame, FRIGATE_EVENT_WAIT_SECONDS, LARGE_IMAGE_WIDTH
            )
            self.hass.bus.async_fire(DETECTION_EVENT, payload)

        task = self.hass.async_create_background_task(announce(), f"surveillance_station detection {review_id}")
        self._announcing.add(task)
        task.add_done_callback(self._announcing.discard)

    def _url(self, camera_id: int, start: int) -> str | None:
        if not self.link:
            return None
        sep = "&" if "?" in self.link else "?"
        # A few seconds before: the card's own event jumps start there too.
        return f"{self.link}{sep}ss_camera={camera_id}&ss_time={start - 3}"
