"""Smart search: Frigate finds, the results are placed on SS (camera, time, the review's bookmark)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator
from synology_ss_playback import Bookmark, Camera

from custom_components.surveillance_station.const import FRIGATE_IMAGE_URL
from custom_components.surveillance_station.frigate import DATA_FRIGATE, FrigateBridge
from custom_components.surveillance_station.frigate_api import FrigateAPI
from custom_components.surveillance_station.views import DATA_MANAGER
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import T0

F = "http://frigate:5000"


def obj(eid: str, camera: str = "drive_way", label: str = "car", start: float = T0 + 100.4, end: float | None = T0 + 110.2):
    return {"id": eid, "camera": camera, "label": label, "start_time": start, "end_time": end}


@pytest.fixture
async def bridge(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, aioclient_mock: AiohttpClientMocker
) -> FrigateBridge:
    mock_client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True), Camera(id=7, name="Backyard", enabled=True)]
    mock_client.list_bookmarks.return_value = [
        Bookmark(id=21, camera_id=6, name="Car", comment="Frigate alert in driveway [frigate r1]", start=T0 + 100, end=T0 + 111),
        Bookmark(id=20, camera_id=6, name="Mine", comment="by hand", start=T0 + 50, end=T0 + 60),
    ]
    b = FrigateBridge(
        hass, setup_integration.entry_id, mock_client, hass.data[DATA_MANAGER], "frigate", {"person", "car"}, "",
        api=FrigateAPI(async_get_clientsession(hass), F),
    )
    hass.data.setdefault(DATA_FRIGATE, {})[setup_integration.entry_id] = b
    return b


async def ask(hass: HomeAssistant, hass_ws_client: WebSocketGenerator, **msg) -> dict:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/search", **msg})
    return await ws.receive_json()


async def test_words(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock: AiohttpClientMocker) -> None:
    """Best first; a review once (its bookmark's name and comment); unmapped, other cameras' and malformed dropped."""
    aioclient_mock.get(f"{F}/api/events/search", json=[
        obj("o1"), obj("o2"), obj("o3", camera="garage"), obj("o4", camera="backyard", label="person"),
        obj("o5", label="dog", end=None), obj(".."), {"id": "o6", "camera": "drive_way"}, "junk",
    ])
    for o, review in (("o1", "r1"), ("o2", "r1"), ("o4", "r4")):
        aioclient_mock.get(f"{F}/api/review/event/{o}", json={"id": review})
    aioclient_mock.get(f"{F}/api/review/event/o5", status=404)  # never a review: listed by itself
    msg = await ask(hass, hass_ws_client, query=" white car ", camera_ids=[6])
    assert msg["success"], msg
    results = msg["result"]["results"]
    assert [r["key"] for r in results] == ["r1", "o5"]
    first, second = results
    assert first == {
        "key": "r1", "event_id": "o1", "review_id": "r1", "camera_id": 6, "label": "car", "kind": "Car",
        "start": T0 + 100, "end": T0 + 111, "bookmark_id": 21, "name": "Car",
        "comment": "Frigate alert in driveway [frigate r1]", "thumbnail": first["thumbnail"],
    }
    assert first["thumbnail"].startswith(f"{FRIGATE_IMAGE_URL}/{bridge.entry_id}/object/o1.webp?exp=")
    assert (second["kind"], second["name"], second["bookmark_id"], second["end"]) == ("Animal", "Animal", None, None)
    search = next(c for c in aioclient_mock.mock_calls if "events/search" in str(c[1]))[1]
    assert dict(search.query) == {"query": "white car", "search_type": "thumbnail,description", "limit": "100"}

    # All cameras: the backyard's too; at most limit.
    msg = await ask(hass, hass_ws_client, query="white car")
    assert [r["key"] for r in msg["result"]["results"]] == ["r1", "r4", "o5"]
    msg = await ask(hass, hass_ws_client, query="white car", limit=1)
    assert [r["key"] for r in msg["result"]["results"]] == ["r1"]


async def test_similar_to_a_bookmark(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock) -> None:
    """By the foremost object of the bookmark's review; that review itself not among the results."""
    aioclient_mock.get(f"{F}/api/review/r1", json={"data": {"detections": ["o0", "o1"]}})
    aioclient_mock.get(f"{F}/api/events/o0", json={"id": "o0", "label": "bicycle"})
    aioclient_mock.get(f"{F}/api/events/o1", json={"id": "o1", "label": "car", "data": {"top_score": 0.9}})
    aioclient_mock.get(f"{F}/api/events/search", json=[obj("o1"), obj("o7", start=T0 + 900)])
    aioclient_mock.get(f"{F}/api/review/event/o1", json={"id": "r1"})
    aioclient_mock.get(f"{F}/api/review/event/o7", json={"id": "r7"})
    msg = await ask(hass, hass_ws_client, bookmark_id=21)
    assert [r["key"] for r in msg["result"]["results"]] == ["r7"]
    search = next(c for c in aioclient_mock.mock_calls if "events/search" in str(c[1]))[1]
    assert dict(search.query) == {"event_id": "o1", "search_type": "similarity", "limit": "100"}


async def test_errors(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock) -> None:
    aioclient_mock.get(f"{F}/api/events/search", status=400, json={"success": False, "message": "Semantic search is not enabled"})
    msg = await ask(hass, hass_ws_client, query="car")
    assert msg["error"] == {"code": "frigate_error", "message": "/api/events/search: HTTP 400 (Semantic search is not enabled)"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/events/search", json={"not": "a list"})
    assert (await ask(hass, hass_ws_client, query="car"))["error"]["code"] == "frigate_error"
    # A bookmark not Frigate's, or gone; Frigate no longer having the review's objects.
    for bookmark_id in (20, 99):
        msg = await ask(hass, hass_ws_client, bookmark_id=bookmark_id)
        assert msg["error"]["code"] == "invalid_format" and "isn't one of Frigate's" in msg["error"]["message"]
    aioclient_mock.get(f"{F}/api/review/r1", json={"data": {"detections": ["o1"]}})
    aioclient_mock.get(f"{F}/api/events/o1", status=404)
    assert "no longer has" in (await ask(hass, hass_ws_client, bookmark_id=21))["error"]["message"]
    # Nothing to search by, both, blank words; no Frigate URL; Frigate detections off.
    for bad in ({}, {"query": "car", "bookmark_id": 21}, {"query": ""}):
        assert (await ask(hass, hass_ws_client, **bad))["error"]["code"] == "invalid_format"
    assert "nothing to search" in (await ask(hass, hass_ws_client, query="  "))["error"]["message"]
    bridge.api = None
    assert "Frigate's URL" in (await ask(hass, hass_ws_client, query="car"))["error"]["message"]
    del hass.data[DATA_FRIGATE][bridge.entry_id]
    assert "Frigate detections" in (await ask(hass, hass_ws_client, query="car"))["error"]["message"]


async def test_cameras_say_search(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/cameras"})
    assert (await ws.receive_json())["result"]["search"] is True
