"""Smart search: Frigate's semantic search, its results put on Surveillance Station.

Frigate finds (it keeps a CLIP embedding of every tracked object's
thumbnail); SS plays. Each result is placed by what the bookmarks are made
by: the SS camera Frigate's camera maps to, and the object's time on SS's
recording. A result on a Frigate bookmark's camera and time is that
bookmark (its name and comment), listed once, by its best-matching object.
"Similar to" a bookmark searches by the foremost object seen in it.

By time, not by Frigate's review ids: Frigate deletes reviews with its
recordings (days) but keeps tracked objects, and so what can be found, as
long as their snapshots (here as long as SS keeps recordings).
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from synology_ss_playback import Bookmark

from .const import FRIGATE_IMAGE_URL, FRIGATE_SEARCH_ASK, FRIGATE_SEARCH_MAX
from .frigate import FrigateBridge, kinds, name_kinds, review_id_of
from .frigate_api import FRIGATE_ID, FrigateAPIError
from .views import VodManager

# The whole search (Frigate's answers and the SS camera list) within this.
SEARCH_TIMEOUT_SECONDS = 15
# An object is a bookmark's when their times overlap, give or take this
# (a review, and so its bookmark, starts when an object qualifies, which is
# after the object itself was first seen).
_SLACK = 2
# "Similar" by time looks back this far for the bookmark's objects: one can
# have started long before its review (a car parked for hours, then moving).
_LOOKBACK = 3600
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
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Results, best first. ValueError: nothing to search by; FrigateAPIError: Frigate's."""
    try:
        async with asyncio.timeout(SEARCH_TIMEOUT_SECONDS):
            return await _search(manager, entry_id, bridge, query, bookmark_id, camera_ids, limit)
    except TimeoutError:
        raise FrigateAPIError("search: no answer in time (Frigate or Surveillance Station)") from None


async def _search(
    manager: VodManager,
    entry_id: str,
    bridge: FrigateBridge,
    query: str | None,
    bookmark_id: int | None,
    camera_ids: list[int] | None,
    limit: int,
) -> list[dict[str, Any]]:
    api = bridge.api
    if api is None:
        raise ValueError("smart search needs Frigate's URL in the integration's options")
    client = manager.client(entry_id)
    if client is None:
        raise KeyError(f"Surveillance Station entry {entry_id} is not loaded")
    bookmarks = [b for b in await manager.bookmarks(entry_id, client) if review_id_of(b.comment)]
    source: Bookmark | None = None
    if bookmark_id is not None:
        source = next((b for b in bookmarks if b.id == bookmark_id), None)
        if source is None:
            raise ValueError("that bookmark isn't one of Frigate's")
        best = await _source_object(bridge, source)
        if best is None:
            raise ValueError("Frigate no longer has that detection")
        params: dict[str, Any] = {"event_id": best["id"], "search_type": "similarity"}
    elif query and query.strip():
        params = {"query": query.strip(), "search_type": "thumbnail,description"}
    else:
        raise ValueError("nothing to search for")
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
        bm = _bookmark_of(obj, camera_id, kind, bookmarks)
        if source is not None and bm is not None and bm.id == source.id:
            continue  # the bookmark searched from
        key = f"b{bm.id}" if bm else str(obj["id"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        results.append(
            {
                "key": key,
                "event_id": obj["id"],
                "camera_id": camera_id,
                "label": label,
                "kind": kind,
                "start": int(start),
                "end": int(end) + 1 if end is not None else None,
                "bookmark_id": bm.id if bm else None,
                "name": bm.name if bm else kind or "Detection",
                "comment": bm.comment if bm else "",
                "thumbnail": manager.sign_path(f"{FRIGATE_IMAGE_URL}/{entry_id}/object/{obj['id']}.webp"),
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
    params: dict[str, Any] = {
        "after": source.start - _LOOKBACK, "before": source.end + _SLACK, "has_snapshot": 1, "limit": 100,
    }
    if names := await bridge.frigate_cameras([source.camera_id]):
        params["cameras"] = ",".join(names)
    near = await api.json("/api/events", params)
    seen = [o for o in near if isinstance(o, dict)] if isinstance(near, list) else []
    return bridge.foremost([o for o in seen if await _placed(bridge, o) == source.camera_id and _overlaps(o, source)])


def _bookmark_of(obj: dict[str, Any], camera_id: int, kind: str, bookmarks: list[Bookmark]) -> Bookmark | None:
    """The Frigate bookmark an object is in: on its camera, of its kind, and of those
    the one it began in, else the one it overlaps most (a car there all day
    overlaps every bookmark of that day)."""
    if not kind:
        return None
    start = _time(obj.get("start_time"))
    if start is None:
        return None
    end = _time(obj.get("end_time")) or time.time()  # still going on
    mine = [
        b for b in bookmarks
        if b.camera_id == camera_id and _overlaps(obj, b) and kind.casefold() in {k.casefold() for k in name_kinds(b.name)}
    ]
    began = [b for b in mine if b.start - _SLACK <= start <= b.end + _SLACK]
    if began:
        return min(began, key=lambda b: abs(b.start - start))
    return max(mine, key=lambda b: min(end, b.end) - max(start, b.start), default=None)


async def _placed(bridge: FrigateBridge, obj: dict[str, Any]) -> int | None:
    """The SS camera a Frigate object was seen on."""
    camera = obj.get("camera")
    found = await bridge.ss_camera(camera) if isinstance(camera, str) and camera else None
    return found[0] if found else None


def _overlaps(obj: dict[str, Any], bm: Bookmark) -> bool:
    start = _time(obj.get("start_time"))
    if start is None:
        return False
    end = _time(obj.get("end_time"))
    if end is None:
        end = time.time()  # still being tracked
    return start <= bm.end + _SLACK and end >= bm.start - _SLACK


def _time(value: Any) -> float | None:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return None
    return t if math.isfinite(t) and 0 < t < 2**32 else None
