"""A notification's image from Frigate's snapshot of the review's foremost object, else SS's frame."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from synology_ss_playback import Bookmark, Camera, SSError

from custom_components.surveillance_station import frigate as frigate_mod
from custom_components.surveillance_station.const import DETECTION_EVENT, FRIGATE_IMAGE_URL
from custom_components.surveillance_station.frigate import DATA_FRIGATE, FrigateBridge
from custom_components.surveillance_station.frigate_api import FrigateAPI, FrigateAPIError
from custom_components.surveillance_station.manager import DATA_MANAGER
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .test_frigate import T, review

F = "http://frigate:5000"
RID = "1790000000.1-abc"
JPG = b"\xff\xd8\xff\xe0snap"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 "


def event(eid: str, label: str, score: float, snapshot: bool = True) -> dict:
    return {"id": eid, "label": label, "has_snapshot": snapshot, "data": {"top_score": score}}


@pytest.fixture(autouse=True)
def clock():
    with patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 2):
        yield


@pytest.fixture
async def bridge(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, aioclient_mock: AiohttpClientMocker
) -> FrigateBridge:
    mock_client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True)]
    mock_client.create_bookmark = AsyncMock(
        side_effect=lambda c, n, s, e, comment="": Bookmark(100, c, n, comment, int(s), int(e))
    )
    manager = hass.data[DATA_MANAGER]
    manager.thumbnail_when_recorded = AsyncMock(return_value=b"ss-frame")
    b = FrigateBridge(
        hass, setup_integration.entry_id, mock_client, manager, "frigate", {"person", "car", "cat"}, "", 5, {"Animal"},
        api=FrigateAPI(async_get_clientsession(hass), F + "/"),
    )
    hass.data.setdefault(DATA_FRIGATE, {})[setup_integration.entry_id] = b
    return b


def mock_review(aioclient_mock: AiohttpClientMocker, events: list[dict], extra_ids=()) -> None:
    ids = [e["id"] for e in events] + list(extra_ids)
    aioclient_mock.get(f"{F}/api/review/{RID}", json={"id": RID, "data": {"detections": ids}})
    for e in events:
        aioclient_mock.get(f"{F}/api/events/{e['id']}", json=e)


async def test_foremost_object_snapshot(bridge: FrigateBridge, aioclient_mock: AiohttpClientMocker) -> None:
    """A person before a car (whatever the scores), then the surest; unchosen labels and no-snapshot objects skipped."""
    mock_review(aioclient_mock, [
        event("e1", "car", 0.97), event("e2", "person", 0.71), event("e3", "person", 0.88),
        event("e4", "person", 0.99, snapshot=False), event("e5", "bicycle", 0.99),
    ], extra_ids=["../../etc", "..", "."])
    aioclient_mock.get(f"{F}/api/events/e3/snapshot.jpg", content=JPG)
    assert await bridge.review_image(RID) == (JPG, "image/jpeg")
    snap = aioclient_mock.mock_calls[-1]
    assert str(snap[1]).startswith(f"{F}/api/events/e3/snapshot.jpg") and snap[1].query == {"bbox": "1", "quality": "90"}
    # Not ids: never in a path (a URL takes ".." as a step up).
    assert [str(c[1]).split("/api/")[1] for c in aioclient_mock.mock_calls[1:-1]] == [f"events/e{i}" for i in range(1, 6)]


async def test_no_snapshot_or_no_answer(bridge: FrigateBridge, aioclient_mock: AiohttpClientMocker) -> None:
    mock_review(aioclient_mock, [event("e1", "bicycle", 0.9)])
    assert await bridge.review_image(RID) is None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{RID}", json={"data": {"detections": ["e1"]}})
    aioclient_mock.get(f"{F}/api/events/e1", status=500)
    with pytest.raises(FrigateAPIError, match="HTTP 500"):
        await bridge.review_image(RID)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{RID}", json=["not", "a", "review"])
    assert await bridge.review_image(RID) is None
    bridge.api = None
    assert await bridge.review_image(RID) is None


async def test_api_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    api = FrigateAPI(async_get_clientsession(hass), F)
    aioclient_mock.get(f"{F}/a", text="<html>")
    aioclient_mock.get(f"{F}/b", json={"x": 1})
    aioclient_mock.get(f"{F}/c", exc=asyncio.TimeoutError())
    with pytest.raises(FrigateAPIError, match="not JSON"):
        await api.json("/a")
    with pytest.raises(FrigateAPIError, match="not a JPEG or WebP") as err:
        await api.image("/b")
    assert err.value.status == 200  # an answer, not what was asked for
    with pytest.raises(FrigateAPIError, match="TimeoutError"):
        await api.json("/c")


async def test_announced_with_frigate_image(hass: HomeAssistant, bridge: FrigateBridge, hass_client_no_auth, aioclient_mock) -> None:
    """The event carries the Frigate image URL and doesn't wait for SS; the URL serves Frigate's snapshot."""
    events = async_capture_events(hass, DETECTION_EVENT)
    with patch.object(frigate_mod, "FRIGATE_SNAPSHOT_SETTLE_SECONDS", 0):
        await bridge.handle(review("new", rid=RID))
        await hass.async_block_till_done()
    assert len(events) == 1
    image = events[0].data["image"]
    assert image.split("?")[0] == f"{FRIGATE_IMAGE_URL}/{bridge.entry_id}/6/{T + 2}/{RID}.jpg"
    bridge.manager.thumbnail_when_recorded.assert_not_awaited()  # no wait for SS's frame

    http = await hass_client_no_auth()
    mock_review(aioclient_mock, [event("e1", "person", 0.9)])
    aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", content=JPG)
    resp = await http.get(image)
    assert resp.status == 200 and await resp.read() == JPG and resp.content_type == "image/jpeg"
    assert (await http.get(image.split("?")[0] + "?exp=1&sig=x")).status == 404  # unsigned
    # A good signature doesn't open another review's, camera's or moment's image.
    path, query = image.split("?")
    for other in (path.replace(RID, "1790000000.2-abc"), path.replace("/6/", "/7/"), path.replace(f"/{T + 2}/", f"/{T + 3}/")):
        assert (await http.get(f"{other}?{query}")).status == 404

    # Frigate down: SS's frame of that moment instead, shown in the diagnostics apart from
    # the bookmarks' errors; and not asked again for a minute (each phone would wait for it).
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{RID}", exc=asyncio.TimeoutError())
    resp = await http.get(image)
    assert resp.status == 200 and await resp.read() == b"ss-frame"
    bridge.manager.thumbnail_when_recorded.assert_awaited_with(bridge.entry_id, 6, T + 2, 13, 1280)
    stats = bridge.stats()
    assert stats["images_frigate"] == 1 and stats["images_ss"] == 1 and stats["images_no_snapshot"] == 0
    assert "TimeoutError" in stats["last_image_error"]["error"] and stats["last_error"] is None
    asked = len(aioclient_mock.mock_calls)
    assert await (await http.get(image)).read() == b"ss-frame"
    assert len(aioclient_mock.mock_calls) == asked
    later = frigate_mod._monotonic() + 61
    with patch.object(frigate_mod, "_monotonic", return_value=later):
        aioclient_mock.clear_requests()
        mock_review(aioclient_mock, [event("e1", "person", 0.9)])
        aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", content=JPG)
        assert await (await http.get(image)).read() == JPG  # back

        # No snapshot there, and SS has no frame either: 404; SS failing: 502. The phone
        # got no image: counted as such, and why.
        aioclient_mock.clear_requests()
        mock_review(aioclient_mock, [])
        bridge.manager.thumbnail_when_recorded.return_value = None
        assert (await http.get(image)).status == 404
        stats = bridge.stats()
        assert stats["images_no_snapshot"] == 1 and stats["images_failed"] == 1 and stats["images_ss"] == 2  # both earlier
        assert stats["last_ss_image_error"]["error"] == "not recorded in time (or SS not answering)"
        assert "TimeoutError" in stats["last_image_error"]["error"]  # Frigate's, not overwritten by SS's
        bridge.manager.thumbnail_when_recorded.side_effect = SSError("Recording", "Download", 400)
        assert (await http.get(image)).status == 502
        assert bridge.stats()["images_failed"] == 2

    # The entry unloaded meanwhile: SS's frame (nothing to ask Frigate with).
    del hass.data[DATA_FRIGATE][bridge.entry_id]
    bridge.manager.thumbnail_when_recorded.side_effect = None
    bridge.manager.thumbnail_when_recorded.return_value = b"ss-frame"
    assert await (await http.get(image)).read() == b"ss-frame"


async def test_without_frigate_api_the_ss_frame(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """No Frigate URL: the SS frame as before, waited for."""
    events = async_capture_events(hass, DETECTION_EVENT)
    bridge.api = None
    await bridge.handle(review("new", rid=RID))
    await hass.async_block_till_done()
    assert "/thumbnail/" in events[0].data["image"] and events[0].data["image"].split("?")[0].endswith("-large.jpg")
    bridge.manager.thumbnail_when_recorded.assert_awaited_once()


async def test_objects_not_written_yet(bridge: FrigateBridge, aioclient_mock: AiohttpClientMocker) -> None:
    """Seconds into a review Frigate may not have written its objects yet (404): their snapshots, from its memory."""
    aioclient_mock.get(f"{F}/api/review/{RID}", json={"data": {"detections": ["e1", "e2"]}})
    aioclient_mock.get(f"{F}/api/events/e1", status=404)
    aioclient_mock.get(f"{F}/api/events/e2", status=404)
    aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", status=404)
    aioclient_mock.get(f"{F}/api/events/e2/snapshot.jpg", content=WEBP)
    assert await bridge.review_image(RID) == (WEBP, "image/webp")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{RID}", json={"data": {"detections": ["e1"]}})
    aioclient_mock.get(f"{F}/api/events/e1", status=404)
    aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", status=404)
    assert await bridge.review_image(RID) is None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{RID}", json={"data": {"detections": ["e1"]}})
    aioclient_mock.get(f"{F}/api/events/e1", status=404)
    aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", status=500)
    with pytest.raises(FrigateAPIError, match="HTTP 500"):
        await bridge.review_image(RID)


async def test_some_objects_failing(bridge: FrigateBridge, aioclient_mock: AiohttpClientMocker) -> None:
    """One object's lookup failing doesn't lose the others; odd answers are skipped, not a crash."""
    aioclient_mock.get(f"{F}/api/review/{RID}", json={"data": {"detections": ["e1", "e2", "e3"]}})
    aioclient_mock.get(f"{F}/api/events/e1", status=500)
    aioclient_mock.get(f"{F}/api/events/e2", json={"id": "e2", "label": ["person"], "has_snapshot": True})
    aioclient_mock.get(f"{F}/api/events/e3", json=event("e3", "car", 0.5))
    aioclient_mock.get(f"{F}/api/events/e3/snapshot.jpg", content=JPG)
    assert await bridge.review_image(RID) == (JPG, "image/jpeg")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{RID}", json={"data": ["not", "a", "dict"]})
    assert await bridge.review_image(RID) is None


async def test_api_bounds(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    api = FrigateAPI(async_get_clientsession(hass), F)
    aioclient_mock.get(f"{F}/s", status=400, json={"success": False, "message": "Semantic search is not enabled"})
    aioclient_mock.get(f"{F}/big", content=b"x" * 64)
    with pytest.raises(FrigateAPIError, match=r"HTTP 400 \(Semantic search is not enabled\)") as err:
        await api.json("/s")
    assert err.value.status == 400
    with patch("custom_components.surveillance_station.frigate_api.MAX_BODY_BYTES", 16), pytest.raises(
        FrigateAPIError, match="too big"
    ):
        await api.json("/big")


async def test_image_budget(hass: HomeAssistant, bridge: FrigateBridge, hass_client_no_auth) -> None:
    """Frigate hanging costs the phone a few seconds, then SS's frame; SS hanging, a 504 in time."""
    image = bridge._image_url(RID, 6, T + 2)

    async def hang(*_):
        await asyncio.sleep(60)

    http = await hass_client_no_auth()
    with patch.object(frigate_mod, "FRIGATE_IMAGE_BUDGET_SECONDS", 0.05), patch.object(bridge, "review_image", hang):
        assert await (await http.get(image)).read() == b"ss-frame"
    assert "TimeoutError" in bridge.stats()["last_image_error"]["error"]
    bridge.manager.thumbnail_when_recorded.side_effect = hang
    with patch.object(frigate_mod, "FRIGATE_IMAGE_FALLBACK_SECONDS", 0.05):
        assert (await http.get(image)).status == 504
    bridge.manager.thumbnail_when_recorded.side_effect = OSError("no ffmpeg")
    assert (await http.get(image)).status == 500
    stats = bridge.stats()
    assert stats["images_failed"] == 2 and stats["last_ss_image_error"]["error"] == "OSError"
    bridge.manager.thumbnail_when_recorded.side_effect = hang
    with patch.object(frigate_mod, "FRIGATE_IMAGE_FALLBACK_SECONDS", 0.05):
        assert (await http.get(image)).status == 504
    stats = bridge.stats()
    assert stats["images_failed"] == 3 and stats["last_ss_image_error"]["error"] == "no frame in time"
    assert "TimeoutError" in stats["last_image_error"]["error"]


async def test_one_review_no_backoff(
    hass: HomeAssistant, bridge: FrigateBridge, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """A review or snapshot Frigate no longer has, or an answer that isn't an image: the next
    object, else SS's frame, and Frigate asked again for the next review (not a minute off)."""
    mock_review(aioclient_mock, [event("e1", "person", 0.9), event("e2", "person", 0.5), event("e3", "car", 0.99)])
    aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", status=404)
    aioclient_mock.get(f"{F}/api/events/e2/snapshot.jpg", json={"not": "an image"})
    aioclient_mock.get(f"{F}/api/events/e3/snapshot.jpg", content=JPG)
    assert await bridge.review_image(RID) == (JPG, "image/jpeg")

    image = bridge._image_url(RID, 6, T + 2)
    http = await hass_client_no_auth()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{RID}", status=404)
    assert await (await http.get(image)).read() == b"ss-frame"
    assert bridge.image_from_frigate() and bridge.stats()["images_no_snapshot"] == 1
    assert bridge.stats()["last_image_error"] is None
    # Refused (a wrong URL: the authenticated port) or failing: Frigate's trouble, shown, a minute off.
    for status in (401, 503):
        bridge._image_down_until = float("-inf")
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{F}/api/review/{RID}", status=status)
        assert await (await http.get(image)).read() == b"ss-frame"
        assert not bridge.image_from_frigate() and f"HTTP {status}" in bridge.stats()["last_image_error"]["error"]

    # A snapshot failing (5xx) midway: not the next object, Frigate's trouble.
    aioclient_mock.clear_requests()
    mock_review(aioclient_mock, [event("e1", "person", 0.9), event("e2", "car", 0.5)])
    aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", status=500)
    with pytest.raises(FrigateAPIError, match="HTTP 500"):
        await bridge.review_image(RID)
    # Three objects tried at most (the phone is waiting), then no snapshot.
    aioclient_mock.clear_requests()
    mock_review(aioclient_mock, [event(f"e{i}", "person", 1 - i / 10) for i in range(1, 5)])
    for i in range(1, 5):
        aioclient_mock.get(f"{F}/api/events/e{i}/snapshot.jpg", status=404)
    assert await bridge.review_image(RID) is None
    assert sum("snapshot.jpg" in str(c[1]) for c in aioclient_mock.mock_calls) == 3


async def test_unloaded_while_settling(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """Stopped during the wait for a better snapshot: no event, and still news for the review's next message."""
    events = async_capture_events(hass, DETECTION_EVENT)
    with patch.object(frigate_mod, "FRIGATE_SNAPSHOT_SETTLE_SECONDS", 3600):
        await bridge.handle(review("new", rid=RID))
        await asyncio.sleep(0)
        bridge.stop()
        await hass.async_block_till_done()
    assert not events and RID in bridge._not_yet


async def test_bookmark_thumbnail(
    hass: HomeAssistant, bridge: FrigateBridge, mock_client: MagicMock, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """A Frigate bookmark's thumbnail is Frigate's snapshot (small, box drawn); once the review is
    gone, of the object Frigate still has from that camera and time; else a redirect to SS's frame."""
    mine = Bookmark(30, 6, "Person", f"Frigate alert [frigate {RID}]", T, T + 20)
    by_hand = Bookmark(31, 6, "Mine", "by hand", T, T + 20)
    mock_client.list_bookmarks.return_value = [mine, by_hand]
    manager = hass.data[DATA_MANAGER]
    assert bridge.bookmark_thumbnail(by_hand) == manager.sign_thumbnail(bridge.entry_id, 6, manager.frame(bridge.entry_id, by_hand))
    url = bridge.bookmark_thumbnail(mine)
    assert url.split("?")[0] == f"{FRIGATE_IMAGE_URL}/{bridge.entry_id}/thumb/6/{manager.frame(bridge.entry_id, mine)}/{RID}.jpg"
    http = await hass_client_no_auth()

    mock_review(aioclient_mock, [event("e1", "person", 0.9)])
    aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", content=JPG)
    resp = await http.get(url)
    assert resp.status == 200 and await resp.read() == JPG and resp.headers["Cache-Control"] == "private, max-age=3600"
    assert aioclient_mock.mock_calls[-1][1].query == {"bbox": "1", "quality": "90", "height": "180"}
    assert (await http.get(url.split("?")[0] + "?exp=1&sig=x")).status == 404

    # The review gone (Frigate keeps reviews days, objects weeks): by camera, time and kind.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{RID}", status=404)
    aioclient_mock.get(f"{F}/api/config", json={"cameras": {"drive_way": {}}})
    aioclient_mock.get(f"{F}/api/events", json=[
        {"id": "o1", "camera": "drive_way", "label": "car", "start_time": T + 1, "end_time": T + 9, "has_snapshot": True},
        {"id": "o2", "camera": "drive_way", "label": "person", "start_time": T + 1, "end_time": T + 9, "has_snapshot": True},
        # Surer, but not in the bookmark's time.
        {"id": "o3", "camera": "drive_way", "label": "person", "start_time": T + 500, "end_time": T + 509,
         "has_snapshot": True, "data": {"top_score": 0.99}},
    ])
    aioclient_mock.get(f"{F}/api/events/o2/snapshot.jpg", content=WEBP)
    resp = await http.get(url)
    assert resp.status == 200 and await resp.read() == WEBP
    assert [str(c[1]).split("?")[0].split("/api/")[1] for c in aioclient_mock.mock_calls][-1] == "events/o2/snapshot.jpg"
    # Gone, so over: that snapshot is kept, not looked for again.
    asked = len(aioclient_mock.mock_calls)
    assert await (await http.get(url)).read() == WEBP and len(aioclient_mock.mock_calls) == asked

    # Another one's review gone, and SS failing to list the bookmarks: SS's frame, and not Frigate's fault.
    rid = "1790000000.2-abc"
    other = Bookmark(32, 6, "Person", f"Frigate alert [frigate {rid}]", T, T + 20)
    mock_client.list_bookmarks.return_value = [mine, by_hand, other]
    url = bridge.bookmark_thumbnail(other)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{rid}", status=404)
    with patch.object(manager, "bookmarks", AsyncMock(side_effect=SSError("Bookmark", "List", 400))):
        resp = await http.get(url, allow_redirects=False)
    assert resp.status == 302 and bridge.thumbs_from_frigate()

    # Nothing of it left, or Frigate down: SS's frame (and not asking Frigate for a minute).
    ss = manager.sign_thumbnail(bridge.entry_id, 6, manager.frame(bridge.entry_id, other))
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{rid}", status=404)
    aioclient_mock.get(f"{F}/api/events", json=[])
    resp = await http.get(url, allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == ss and bridge.image_from_frigate()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{rid}", status=503)
    resp = await http.get(url, allow_redirects=False)
    assert resp.status == 302 and not bridge.thumbs_from_frigate()
    # The notifications' own: they find out for themselves (their lookup is lighter).
    assert bridge.image_from_frigate() and bridge.stats()["last_image_error"] is None
    assert bridge.stats()["images_frigate"] == 0
    asked = len(aioclient_mock.mock_calls)
    assert (await http.get(url, allow_redirects=False)).status == 302
    assert len(aioclient_mock.mock_calls) == asked


async def test_thumbnails_of_reviews_that_are_over_are_kept(
    bridge: FrigateBridge, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """Asked for by every page of the event list on every device: a review that is over has its
    thumbnail asked of Frigate once; one still going on (its snapshot may get better) every time."""
    url = bridge.bookmark_thumbnail(Bookmark(30, 6, "Person", f"Frigate alert [frigate {RID}]", T, T + 20))
    http = await hass_client_no_auth()
    mock_review(aioclient_mock, [event("e1", "person", 0.9)])  # no end_time: going on
    aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", content=JPG)
    for _ in range(2):
        assert await (await http.get(url)).read() == JPG
    assert len(aioclient_mock.mock_calls) == 6  # the review, its object, the snapshot: each time

    def ended(rid: str, body: bytes) -> None:
        aioclient_mock.get(f"{F}/api/review/{rid}", json={"id": rid, "end_time": T + 20, "data": {"detections": [f"o{rid}"]}})
        aioclient_mock.get(f"{F}/api/events/o{rid}", json=event(f"o{rid}", "person", 0.9))
        aioclient_mock.get(f"{F}/api/events/o{rid}/snapshot.jpg", content=body)

    aioclient_mock.clear_requests()
    ended(RID, JPG)
    for _ in range(3):
        assert await (await http.get(url)).read() == JPG
    assert len(aioclient_mock.mock_calls) == 3  # once

    # Bounded by size: the least recently used goes first.
    rid = "1790000000.2-abc"
    other = bridge.bookmark_thumbnail(Bookmark(31, 6, "Person", f"Frigate alert [frigate {rid}]", T, T + 20))
    ended(rid, WEBP)
    with patch.object(frigate_mod, "FRIGATE_THUMB_CACHE_BYTES", len(JPG) + len(WEBP) - 1):
        assert await (await http.get(other)).read() == WEBP
        asked = len(aioclient_mock.mock_calls)
        assert await (await http.get(other)).read() == WEBP and len(aioclient_mock.mock_calls) == asked
        assert await (await http.get(url)).read() == JPG and len(aioclient_mock.mock_calls) == asked + 3


async def test_thumbnails_queued(hass: HomeAssistant, bridge: FrigateBridge, hass_client_no_auth) -> None:
    """A page's thumbnails wait their turn outside the budget: a queue isn't Frigate being slow."""
    mine = Bookmark(30, 6, "Person", f"Frigate alert [frigate {RID}]", T, T + 20)
    url = bridge.bookmark_thumbnail(mine)
    http = await hass_client_no_auth()

    async def slow(*_):
        await asyncio.sleep(0.05)
        return JPG, "image/jpeg"

    with patch.object(frigate_mod, "FRIGATE_IMAGE_BUDGET_SECONDS", 0.1), patch.object(bridge, "bookmark_image", slow), \
            patch.object(bridge, "thumb_sem", asyncio.Semaphore(1)):
        answers = await asyncio.gather(*(http.get(url, allow_redirects=False) for _ in range(6)))
    assert [r.status for r in answers] == [200] * 6 and bridge.thumbs_from_frigate()


async def test_thumbnail_ss_camera_list_failing(
    hass: HomeAssistant, bridge: FrigateBridge, mock_client: MagicMock, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """SS failing to list its cameras (to place Frigate's objects): SS's frame, Frigate not blamed."""
    mine = Bookmark(30, 6, "Person", f"Frigate alert [frigate {RID}]", T, T + 20)
    mock_client.list_bookmarks.return_value = [mine]
    url = bridge.bookmark_thumbnail(mine)
    aioclient_mock.get(f"{F}/api/review/{RID}", status=404)
    aioclient_mock.get(f"{F}/api/config", json={"cameras": {"drive_way": {}}})
    aioclient_mock.get(f"{F}/api/events", json=[
        {"id": "o2", "camera": "drive_way", "label": "person", "start_time": T + 1, "end_time": T + 9, "has_snapshot": True},
    ])
    bridge._cameras_at = float("-inf")
    # The event list's bookmarks (SS's cameras) answer; the bridge's own look at the cameras fails.
    mock_client.cameras.side_effect = [[Camera(id=6, name="Drive Way", enabled=True)], SSError("Camera", "List", 400)]
    http = await hass_client_no_auth()
    resp = await http.get(url, allow_redirects=False)
    assert resp.status == 302 and bridge.thumbs_from_frigate() and mock_client.cameras.call_count == 2

