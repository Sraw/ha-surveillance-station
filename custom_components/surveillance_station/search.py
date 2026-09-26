"""Smart search: Frigate's semantic search, its results put on Surveillance Station.

Frigate finds (it keeps a CLIP embedding of every tracked object's
thumbnail); SS plays. Each result is placed by what the bookmarks are made
by: the SS camera Frigate's camera maps to, and the object's start time on
SS's recording. A result whose review has a bookmark is that bookmark (its
name, comment and id), and a review is listed once, by its best-matching
object. "Similar to" a bookmark searches by the foremost object of its
review.

Frigate deletes a tracked object (and its embedding) with its snapshot, so
what can be found is what Frigate's snapshot retention keeps.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .const import FRIGATE_IMAGE_URL, FRIGATE_SEARCH_ASK, FRIGATE_SEARCH_MAX
from .frigate import FrigateBridge, kinds, review_id_of
from .frigate_api import FRIGATE_ID, FrigateAPIError
from .views import VodManager

# Frigate asked about this many results' reviews at once.
_REVIEW_LOOKUPS = 8


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
    api = bridge.api
    if api is None:
        raise ValueError("smart search needs Frigate's URL in the integration's options")
    client = manager.client(entry_id)
    if client is None:
        raise KeyError(f"Surveillance Station entry {entry_id} is not loaded")
    bookmarks = await manager.bookmarks(entry_id, client)
    by_review = {rid: b for b in bookmarks if (rid := review_id_of(b.comment))}
    source: str | None = None
    if bookmark_id is not None:
        bm = next((b for b in bookmarks if b.id == bookmark_id), None)
        if bm is None or (source := review_id_of(bm.comment)) is None:
            raise ValueError("that bookmark isn't one of Frigate's")
        review = await api.json(f"/api/review/{source}")
        ids = [i for i in _detections(review) if FRIGATE_ID.fullmatch(i)]
        objects = await asyncio.gather(*(api.json(f"/api/events/{i}") for i in ids[:16]), return_exceptions=True)
        best = bridge.foremost(list(objects))
        if best is None:
            raise ValueError("Frigate no longer has that detection")
        params = {"event_id": best["id"], "search_type": "similarity"}
    elif query and query.strip():
        params = {"query": query.strip(), "search_type": "thumbnail,description"}
    else:
        raise ValueError("nothing to search for")
    found = await api.json("/api/events/search", {**params, "limit": FRIGATE_SEARCH_ASK})
    if not isinstance(found, list):
        raise FrigateAPIError("/api/events/search: not a list")

    cameras: dict[str, tuple[int, str] | None] = {}
    candidates = []
    for obj in found:
        if not isinstance(obj, dict) or not FRIGATE_ID.fullmatch(str(obj.get("id"))):
            continue
        name = str(obj.get("camera") or "")
        if name not in cameras:
            cameras[name] = await bridge.ss_camera(name)
        if (camera := cameras[name]) is None or (camera_ids is not None and camera[0] not in camera_ids):
            continue
        try:
            start = float(obj["start_time"])
        except (KeyError, TypeError, ValueError):
            continue
        candidates.append((obj, camera, start))

    # Each object's review (a review is listed once); a few at a time.
    candidates = candidates[: FRIGATE_SEARCH_ASK]
    gate = asyncio.Semaphore(_REVIEW_LOOKUPS)

    async def review_of(obj: dict[str, Any]) -> str | None:
        async with gate:
            try:
                review = await api.json(f"/api/review/event/{obj['id']}")
            except FrigateAPIError:
                return None  # no review (never an alert or detection): listed by itself
        rid = review.get("id") if isinstance(review, dict) else None
        return rid if isinstance(rid, str) and FRIGATE_ID.fullmatch(rid) else None

    reviews = await asyncio.gather(*(review_of(obj) for obj, _, _ in candidates))
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for (obj, (camera_id, _), start), rid in zip(candidates, reviews, strict=True):
        key = rid or obj["id"]
        if key in seen or (source is not None and rid == source):
            continue
        seen.add(key)
        label = str(obj.get("label") or "")
        bm = by_review.get(rid) if rid else None
        try:
            end = int(float(obj["end_time"])) + 1 if obj.get("end_time") else None
        except (TypeError, ValueError):
            end = None
        results.append(
            {
                "key": key,
                "event_id": obj["id"],
                "review_id": rid,
                "camera_id": camera_id,
                "label": label,
                "kind": (kinds([label]) or [""])[0],
                "start": int(start),
                "end": end,
                "bookmark_id": bm.id if bm else None,
                "name": bm.name if bm else (kinds([label]) or ["Detection"])[0],
                "comment": bm.comment if bm else "",
                "thumbnail": manager.sign_path(f"{FRIGATE_IMAGE_URL}/{entry_id}/object/{obj['id']}.webp"),
            }
        )
        if len(results) >= min(limit, FRIGATE_SEARCH_MAX):
            break
    return results


def _detections(review: Any) -> list[str]:
    data = review.get("data") if isinstance(review, dict) else None
    ids = data.get("detections") if isinstance(data, dict) else None
    return [str(i) for i in ids] if isinstance(ids, list) else []
