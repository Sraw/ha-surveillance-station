"""Smart search: Frigate's semantic search, its results put on Surveillance Station.

Frigate finds (it keeps a CLIP embedding of every tracked object's
thumbnail); SS plays. Each result is placed by what the bookmarks are made
by: the SS camera Frigate's camera maps to, and the object's time on SS's
recording. Only what is a Frigate bookmark (of its kind, on its camera
then) is a result, listed once, by its best-matching object: what the event
list and the timeline hold, not what Frigate saw and let go (outside the
zones that count, a kind not bookmarked).
"Similar to" a bookmark searches by the foremost object seen in it.

By time, not by Frigate's review ids: Frigate deletes reviews with its
recordings (days) but keeps tracked objects, and so what can be found, as
long as their snapshots (here as long as SS keeps recordings).
"""

from __future__ import annotations

import asyncio
from bisect import bisect_left, bisect_right
import math
import time
from typing import Any

from synology_ss_playback import Bookmark

from .const import FRIGATE_SEARCH_ASK, FRIGATE_SEARCH_MAX
from .errors import EntryNotLoaded, InvalidRequest
from .frigate import BOOKMARK_SLACK, FrigateBridge, event_time as _time, kinds, name_kinds, review_id_of
from .frigate_api import FRIGATE_ID, FrigateAPIError
from .views import VodManager

# The whole search (Frigate's answers and the SS camera list) within this.
SEARCH_TIMEOUT_SECONDS = 15
# An object is a bookmark's when their times overlap, give or take this.
_SLACK = BOOKMARK_SLACK
# Frigate (0.18) may answer a search that ran into another client's with
# nothing: an empty answer is asked once more, this much later.
_EMPTY_RETRY_SECONDS = 0.5


async def search(
    manager: VodManager,
    entry_id: str,
    bridge: FrigateBridge,
    *,
    query: str | None = None,
    bookmark_id: int | None = None,
    camera_ids: list[int] | None = None,
    kinds: list[str] | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Results, best first: bookmarks (of these kinds, if any given). InvalidRequest: nothing
    to search by; FrigateAPIError: Frigate's."""
    wanted = {k.strip().casefold() for k in kinds or [] if k.strip()}
    try:
        async with asyncio.timeout(SEARCH_TIMEOUT_SECONDS):
            return await _search(manager, entry_id, bridge, query, bookmark_id, camera_ids, wanted, limit)
    except TimeoutError:
        raise FrigateAPIError("search: no answer in time (Frigate or Surveillance Station)") from None


async def _search(
    manager: VodManager,
    entry_id: str,
    bridge: FrigateBridge,
    query: str | None,
    bookmark_id: int | None,
    camera_ids: list[int] | None,
    wanted: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    api = bridge.api
    if api is None:
        raise InvalidRequest("smart search needs Frigate's URL in the integration's options")
    client = manager.client(entry_id)
    if client is None:
        raise EntryNotLoaded(f"Surveillance Station entry {entry_id} is not loaded")
    bookmarks = [b for b in await manager.bookmarks(entry_id, client) if review_id_of(b.comment)]
    source: Bookmark | None = None
    if bookmark_id is not None:
        source = next((b for b in bookmarks if b.id == bookmark_id), None)
        if source is None:
            raise InvalidRequest("that bookmark isn't one of Frigate's")
        best = await _source_object(bridge, source)
        if best is None:
            raise InvalidRequest("Frigate no longer has that detection")
        params: dict[str, Any] = {"event_id": best["id"], "search_type": "similarity"}
    elif query and query.strip():
        params = {"query": query.strip(), "search_type": "thumbnail,description"}
    else:
        raise InvalidRequest("nothing to search for")
    if camera_ids is not None and (names := await bridge.frigate_cameras(camera_ids)) is not None:
        if not names:
            return []
        params["cameras"] = ",".join(names)
    async with bridge.search_lock:
        found = await api.json("/api/events/search", {**params, "limit": FRIGATE_SEARCH_ASK})
        if found == []:
            await asyncio.sleep(_EMPTY_RETRY_SECONDS)
            found = await api.json("/api/events/search", {**params, "limit": FRIGATE_SEARCH_ASK})
    if not isinstance(found, list):
        raise FrigateAPIError("/api/events/search: not a list")

    results: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    index = _Index(bookmarks)
    for obj in found:
        if not isinstance(obj, dict) or not FRIGATE_ID.fullmatch(str(obj.get("id"))):
            continue
        camera_id = await _placed(bridge, obj)
        start = _time(obj.get("start_time"))
        if camera_id is None or start is None or (camera_ids is not None and camera_id not in camera_ids):
            continue
        end = _time(obj.get("end_time"))
        label = obj.get("label") if isinstance(obj.get("label"), str) else ""
        kind = (kinds([label]) or [""])[0] if label else ""
        bm = index.bookmark_of(camera_id, kind, start, time.time() if end is None else end)
        if bm is None or (source is not None and bm.id == source.id):
            continue  # not an event (no bookmark), or the bookmark searched from
        key = f"b{bm.id}"
        if key in seen_keys or (wanted and not wanted & index.kinds[bm.id]):
            continue
        seen_keys.add(key)
        # The bookmark's stretch of the object (a car parked since the morning, in a
        # bookmark of the afternoon): the result plays the event, not the morning.
        if start < bm.start - _SLACK:
            start = bm.start
        if end is None or end > bm.end + _SLACK:
            end = bm.end
        results.append(
            {
                "key": key,
                "event_id": obj["id"],
                "camera_id": camera_id,
                "label": label,
                "kind": kind,
                "start": int(start),
                "end": math.ceil(end) if end is not None else None,
                "bookmark_id": bm.id,
                "name": bm.name,
                "comment": bm.comment,
                # The bookmark's own, as the event list shows it (Frigate's snapshot, box drawn),
                # not Frigate's crop of the object (a small square).
                "thumbnail": bridge.bookmark_thumbnail(bm),
            }
        )
        if len(results) >= min(limit, FRIGATE_SEARCH_MAX):
            break
    return results


async def _source_object(bridge: FrigateBridge, source: Bookmark) -> dict[str, Any] | None:
    """The foremost object seen in a Frigate bookmark: by its review while Frigate
    has it (exact), else by the bookmark's camera and time."""
    api = bridge.api
    assert api is not None
    rid = review_id_of(source.comment)
    try:
        review = await api.json(f"/api/review/{rid}")
    except FrigateAPIError as err:
        if err.status != 404:
            raise
        review = None
    data = review.get("data") if isinstance(review, dict) else None
    ids = data.get("detections") if isinstance(data, dict) else None
    ids = [i for i in ids if isinstance(i, str) and FRIGATE_ID.fullmatch(i)][:16] if isinstance(ids, list) else []
    if ids:
        found = await asyncio.gather(*(api.json(f"/api/events/{i}") for i in ids), return_exceptions=True)
        if (best := bridge.foremost([o for o in found if isinstance(o, dict)])) is not None:
            return best
    ranked = await bridge.bookmark_objects(source)
    return ranked[0] if ranked else None


class _Index:
    """The Frigate bookmarks by camera, in start order, and each one's kinds (casefolded):
    up to 100 results each looked up by bisecting, not by going through tens of
    thousands of bookmarks once per result."""

    def __init__(self, bookmarks: list[Bookmark]) -> None:
        self.kinds = {b.id: frozenset(k.casefold() for k in name_kinds(b.name)) for b in bookmarks}
        self._marks: dict[int, list[Bookmark]] = {}
        for b in bookmarks:
            self._marks.setdefault(b.camera_id, []).append(b)
        self._starts: dict[int, list[int]] = {}
        self._longest: dict[int, int] = {}
        for camera_id, marks in self._marks.items():
            marks.sort(key=lambda b: (b.start, b.id))
            self._starts[camera_id] = [b.start for b in marks]
            self._longest[camera_id] = max(b.end - b.start for b in marks)

    def bookmark_of(self, camera_id: int, kind: str, start: float, end: float) -> Bookmark | None:
        """The Frigate bookmark an object (there from start to end) is in: on its camera,
        of its kind, and of those the one it began in, else the one it overlaps most (a
        car there all day overlaps every bookmark of that day)."""
        if not kind or (marks := self._marks.get(camera_id)) is None:
            return None
        starts, kind = self._starts[camera_id], kind.casefold()
        # Overlapping: begun by the object's end, and ended after its start, so begun
        # no earlier than that less the camera's longest bookmark. Newest first, as SS
        # lists them: of two as near, the newer.
        lo = bisect_left(starts, start - _SLACK - self._longest[camera_id])
        hi = bisect_right(starts, end + _SLACK)
        mine = [b for b in reversed(marks[lo:hi]) if b.end >= start - _SLACK and kind in self.kinds[b.id]]
        began = [b for b in mine if b.start - _SLACK <= start <= b.end + _SLACK]
        if began:
            return min(began, key=lambda b: abs(b.start - start))
        return max(mine, key=lambda b: min(end, b.end) - max(start, b.start), default=None)


async def _placed(bridge: FrigateBridge, obj: dict[str, Any]) -> int | None:
    """The SS camera a Frigate object was seen on."""
    camera = obj.get("camera")
    found = await bridge.ss_camera(camera) if isinstance(camera, str) and camera else None
    return found[0] if found else None
