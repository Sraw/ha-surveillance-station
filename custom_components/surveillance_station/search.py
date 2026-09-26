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
from typing import Any

from synology_ss_playback import Bookmark

from .const import FRIGATE_IMAGE_URL, FRIGATE_SEARCH_ASK, FRIGATE_SEARCH_MAX
from .frigate import FrigateBridge, kinds, review_id_of
from .frigate_api import FRIGATE_ID, FrigateAPIError
from .views import VodManager

# The whole search (Frigate's answers and the SS camera list) within this.
SEARCH_TIMEOUT_SECONDS = 15
# An object is a bookmark's when their times overlap, give or take this
# (a review, and so its bookmark, starts when an object qualifies, which is
# after the object itself was first seen).
_SLACK = 2


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
        raise FrigateAPIError("search: no answer in time") from None


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
        # The objects Frigate saw on that camera then (by time: its review may be gone).
        near = await api.json(
            "/api/events",
            {"after": source.start - 60, "before": source.end + _SLACK, "has_snapshot": 1, "limit": 100},
        )
        seen = [o for o in near if isinstance(o, dict)] if isinstance(near, list) else []
        mine = [o for o in seen if await _placed(bridge, o) == source.camera_id and _overlaps(o, source)]
        best = bridge.foremost(mine)
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
        bm = next(
            (b for b in bookmarks if b.camera_id == camera_id and _overlaps(obj, b)
             and (source is None or b.id != source.id)),
            None,
        )
        if source is not None and _overlaps(obj, source) and camera_id == source.camera_id:
            continue  # the bookmark searched from
        key = f"b{bm.id}" if bm else str(obj["id"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        label = obj.get("label") if isinstance(obj.get("label"), str) else ""
        kind = (kinds([label]) or [""])[0] if label else ""
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
    return start <= bm.end + _SLACK and (end if end is not None else start) >= bm.start - _SLACK


def _time(value: Any) -> float | None:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return None
    return t if math.isfinite(t) and 0 < t < 2**32 else None
