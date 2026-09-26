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
from custom_components.surveillance_station.views import DATA_MANAGER
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .test_frigate import T, review

F = "http://frigate:5000"
RID = "1790000000.1-abc"


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
    ], extra_ids=["../../etc"])
    aioclient_mock.get(f"{F}/api/events/e3/snapshot.jpg", content=b"snap", headers={"Content-Type": "image/jpeg"})
    assert await bridge.review_image(RID) == (b"snap", "image/jpeg")
    snap = aioclient_mock.mock_calls[-1]
    assert str(snap[1]).startswith(f"{F}/api/events/e3/snapshot.jpg") and snap[1].query == {"bbox": "1", "quality": "90"}
    assert not any("etc" in str(c[1]) for c in aioclient_mock.mock_calls)  # not an id: never in a path


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
    with pytest.raises(FrigateAPIError, match="not an image"):
        await api.image("/b")
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
    aioclient_mock.get(f"{F}/api/events/e1/snapshot.jpg", content=b"snap", headers={"Content-Type": "image/jpeg"})
    resp = await http.get(image)
    assert resp.status == 200 and await resp.read() == b"snap"
    assert (await http.get(image.split("?")[0] + "?exp=1&sig=x")).status == 404  # unsigned

    # Frigate down: SS's frame of that moment instead, and it shows in the diagnostics.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/{RID}", exc=asyncio.TimeoutError())
    resp = await http.get(image)
    assert resp.status == 200 and await resp.read() == b"ss-frame"
    bridge.manager.thumbnail_when_recorded.assert_awaited_with(bridge.entry_id, 6, T + 2, 20, 1280)
    stats = bridge.stats()
    assert stats["images_frigate"] == 1 and stats["images_ss"] == 1 and "TimeoutError" in stats["last_error"]["error"]

    # No snapshot there, and SS has no frame either: 404; SS failing: 502.
    aioclient_mock.clear_requests()
    mock_review(aioclient_mock, [])
    bridge.manager.thumbnail_when_recorded.return_value = None
    assert (await http.get(image)).status == 404
    bridge.manager.thumbnail_when_recorded.side_effect = SSError("Recording", "Download", 400)
    assert (await http.get(image)).status == 502

    # The entry unloaded meanwhile: SS's frame (nothing to ask Frigate with).
    del hass.data[DATA_FRIGATE][bridge.entry_id]
    bridge.manager.thumbnail_when_recorded.side_effect = None
    bridge.manager.thumbnail_when_recorded.return_value = b"ss-frame"
    assert await (await http.get(image)).read() == b"ss-frame"


async def test_without_frigate_api_the_ss_frame(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """No Frigate URL (or an id not fit for a path): the SS frame as before, waited for."""
    events = async_capture_events(hass, DETECTION_EVENT)
    bridge.api = None
    await bridge.handle(review("new", rid=RID))
    await hass.async_block_till_done()
    assert "/thumbnail/" in events[0].data["image"] and events[0].data["image"].split("?")[0].endswith("-large.jpg")
    bridge.manager.thumbnail_when_recorded.assert_awaited_once()
