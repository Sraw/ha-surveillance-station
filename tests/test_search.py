"""Smart search: Frigate finds, the results are placed on SS (camera, time, the bookmark then)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator
from synology_ss_playback import Bookmark, Camera

from custom_components.surveillance_station import frigate as frigate_mod, search as search_mod
from custom_components.surveillance_station.const import FRIGATE_IMAGE_URL
from custom_components.surveillance_station.frigate import DATA_FRIGATE, FrigateBridge
from custom_components.surveillance_station.frigate_api import FrigateAPI
from custom_components.surveillance_station.views import DATA_MANAGER
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import T0

F = "http://frigate:5000"
CONFIG = {"cameras": {"drive_way": {}, "backyard": {}, "garage": {}}}


def obj(eid: str, camera: str = "drive_way", label: str = "car", start: float = T0 + 98.4, end: float | None = T0 + 110.2):
    return {"id": eid, "camera": camera, "label": label, "start_time": start, "end_time": end}


@pytest.fixture
async def bridge(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, aioclient_mock: AiohttpClientMocker
) -> FrigateBridge:
    mock_client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True), Camera(id=7, name="Backyard", enabled=True)]
    mock_client.list_bookmarks.return_value = [
        Bookmark(id=23, camera_id=7, name="Person", comment="Frigate alert [frigate r3]", start=T0 + 95, end=T0 + 120),
        Bookmark(id=22, camera_id=6, name="Person", comment="Frigate alert [frigate r2]", start=T0 + 900, end=T0 + 920),
        Bookmark(id=21, camera_id=6, name="Car", comment="Frigate alert in driveway [frigate r1]", start=T0 + 100, end=T0 + 111),
        Bookmark(id=20, camera_id=6, name="Mine", comment="by hand", start=T0 + 50, end=T0 + 120),
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


def query_of(aioclient_mock: AiohttpClientMocker, path: str) -> dict:
    return dict(next(c for c in reversed(aioclient_mock.mock_calls) if path in str(c[1]))[1].query)


async def test_words(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock: AiohttpClientMocker) -> None:
    """Best first; a bookmark once, by its camera and time (not Frigate's review ids); what isn't
    a Frigate bookmark (a kind not bookmarked, a hand-made bookmark's time, unplaceable) isn't a result."""
    aioclient_mock.get(f"{F}/api/config", json=CONFIG)
    aioclient_mock.get(f"{F}/api/events/search", json=[
        obj("o1"), obj("o2", start=T0 + 105), obj("o3", camera="garage"), obj("o4", camera="backyard", label="person"),
        obj("o5", label="dog", start=T0 + 500, end=None), obj(".."), {"id": "o6", "camera": "drive_way"},
        obj("o7", start="1e400"), obj("o8", label=["car"], start=T0 + 700), "junk",
    ])
    msg = await ask(hass, hass_ws_client, query=" white car ", camera_ids=[6])
    assert msg["success"], msg
    results = msg["result"]["results"]
    assert [r["key"] for r in results] == ["b21"]
    first = results[0]
    assert first == {
        "key": "b21", "event_id": "o1", "camera_id": 6, "label": "car", "kind": "Car",
        "start": T0 + 98, "end": T0 + 111, "bookmark_id": 21, "name": "Car",
        "comment": "Frigate alert in driveway [frigate r1]", "thumbnail": first["thumbnail"],
    }  # not the hand-made bookmark over the same time
    assert first["thumbnail"].startswith(f"{FRIGATE_IMAGE_URL}/{bridge.entry_id}/object/o1.webp?exp=")
    # Frigate asked for the cameras shown only (its names for them).
    assert query_of(aioclient_mock, "events/search") == {
        "query": "white car", "search_type": "thumbnail,description", "limit": "100", "cameras": "drive_way",
    }

    # All cameras: the backyard's too; at most limit.
    msg = await ask(hass, hass_ws_client, query="white car")
    assert [r["key"] for r in msg["result"]["results"]] == ["b21", "b23"]
    assert "cameras" not in query_of(aioclient_mock, "events/search")
    msg = await ask(hass, hass_ws_client, query="white car", limit=1)
    assert [r["key"] for r in msg["result"]["results"]] == ["b21"]


async def test_cameras_filter(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock) -> None:
    """A camera Frigate doesn't have: nothing (not asked); Frigate's list unknown: filtered after."""
    aioclient_mock.get(f"{F}/api/config", json={"cameras": {"garage": {}}})
    msg = await ask(hass, hass_ws_client, query="car", camera_ids=[6])
    assert msg["result"]["results"] == [] and not any("events/search" in str(c[1]) for c in aioclient_mock.mock_calls)
    aioclient_mock.clear_requests()
    bridge._frigate_cameras_at = float("-inf")
    aioclient_mock.get(f"{F}/api/config", status=500)
    aioclient_mock.get(f"{F}/api/events/search", json=[obj("o1"), obj("o4", camera="backyard", label="person")])
    msg = await ask(hass, hass_ws_client, query="car", camera_ids=[7])
    assert [r["key"] for r in msg["result"]["results"]] == ["b23"]
    assert "cameras" not in query_of(aioclient_mock, "events/search")


async def test_similar_by_review(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock) -> None:
    """While Frigate has the bookmark's review: its foremost object, exactly."""
    aioclient_mock.get(f"{F}/api/review/r1", json={"data": {"detections": ["o0", "o1"]}})
    aioclient_mock.get(f"{F}/api/events/o0", json={"id": "o0", "label": "bicycle"})
    aioclient_mock.get(f"{F}/api/events/o1", json={"id": "o1", "label": "car"})
    aioclient_mock.get(f"{F}/api/events/search", json=[obj("o1"), obj("o7", start=T0 + 905, end=T0 + 915, label="person")])
    msg = await ask(hass, hass_ws_client, bookmark_id=21)
    assert [r["key"] for r in msg["result"]["results"]] == ["b22"]
    assert query_of(aioclient_mock, "events/search")["event_id"] == "o1"
    assert not any(str(c[1]).endswith("/api/events") for c in aioclient_mock.mock_calls)


async def test_similar_to_a_bookmark(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock) -> None:
    """Its review gone: the foremost object Frigate saw on that camera then; that bookmark not a result."""
    aioclient_mock.get(f"{F}/api/review/r1", status=404)
    aioclient_mock.get(f"{F}/api/config", json=CONFIG)
    aioclient_mock.get(f"{F}/api/events", json=[
        obj("o0", label="bicycle"), obj("o9", camera="backyard", label="person"), obj("o1", label="car"),
        obj("o2", label="car", start=T0 + 300, end=T0 + 310),  # on the camera, not in the bookmark's time
    ])
    aioclient_mock.get(f"{F}/api/events/search", json=[obj("o1"), obj("o7", start=T0 + 905, end=T0 + 915, label="person")])
    msg = await ask(hass, hass_ws_client, bookmark_id=21)
    assert [r["key"] for r in msg["result"]["results"]] == ["b22"]
    assert query_of(aioclient_mock, "events/search") == {"event_id": "o1", "search_type": "similarity", "limit": "100"}
    # Back an hour (a car there long before its review), on that camera only.
    assert query_of(aioclient_mock, "/api/events?") == {
        "after": str(T0 + 100 - 3600), "before": str(T0 + 113), "has_snapshot": "1", "limit": "100", "cameras": "drive_way",
    }


async def test_which_bookmark(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock) -> None:
    """A car there all day is the car bookmark it overlaps (not the newest one of the day, a person's); one
    still being tracked, begun before its bookmark, is that bookmark; an object of no bookmark's kind: no result."""
    aioclient_mock.get(f"{F}/api/events/search", json=[
        obj("o1", start=T0 + 50, end=T0 + 5000),
        obj("o2", label="person", start=T0 + 895, end=None),
        obj("o3", label="dog", start=T0 + 905, end=T0 + 910),
    ])
    with patch.object(search_mod.time, "time", return_value=T0 + 950):
        msg = await ask(hass, hass_ws_client, query="car")
    assert [(r["key"], r["name"]) for r in msg["result"]["results"]] == [("b21", "Car"), ("b22", "Person")]


async def test_empty_answer_asked_again(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock) -> None:
    """Frigate answering nothing (another client's search at the same time): asked once more."""
    answers = iter([[], [obj("o1")]])

    async def frigate(path, params=None):
        return next(answers) if "search" in path else {}

    with patch.object(bridge.api, "json", frigate), patch.object(search_mod, "_EMPTY_RETRY_SECONDS", 0):
        msg = await ask(hass, hass_ws_client, query="car")
    assert [r["key"] for r in msg["result"]["results"]] == ["b21"]


async def test_frigate_cameras_listed_again(hass: HomeAssistant, bridge: FrigateBridge, aioclient_mock) -> None:
    """A camera Frigate didn't have: its list asked again after a minute (added or renamed), not every search."""
    aioclient_mock.get(f"{F}/api/config", json={"cameras": {"drive_way": {}}})
    assert await bridge.frigate_cameras([6, 7]) == ["drive_way"]
    assert await bridge.frigate_cameras([6, 7]) == ["drive_way"]
    assert len(aioclient_mock.mock_calls) == 1
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/config", json=CONFIG)
    later = frigate_mod._monotonic() + 61
    with patch.object(frigate_mod, "_monotonic", return_value=later):
        assert await bridge.frigate_cameras([6, 7]) == ["drive_way", "backyard"]


async def test_errors(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client, aioclient_mock) -> None:
    aioclient_mock.get(f"{F}/api/events/search", status=400, json={"success": False, "message": "Semantic search is not enabled"})
    msg = await ask(hass, hass_ws_client, query="car")
    assert msg["error"] == {"code": "frigate_error", "message": "/api/events/search: HTTP 400 (Semantic search is not enabled)"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/events/search", json={"not": "a list"})
    assert (await ask(hass, hass_ws_client, query="car"))["error"]["code"] == "frigate_error"
    # A bookmark not Frigate's, or gone; Frigate no longer having what it saw then.
    for bookmark_id in (20, 99):
        msg = await ask(hass, hass_ws_client, bookmark_id=bookmark_id)
        assert msg["error"]["code"] == "invalid_format" and "isn't one of Frigate's" in msg["error"]["message"]
    aioclient_mock.get(f"{F}/api/review/r1", status=404)
    aioclient_mock.get(f"{F}/api/config", status=500)
    aioclient_mock.get(f"{F}/api/events", json={"odd": "answer"})
    assert "no longer has" in (await ask(hass, hass_ws_client, bookmark_id=21))["error"]["message"]
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{F}/api/review/r1", status=500)
    assert (await ask(hass, hass_ws_client, bookmark_id=21))["error"]["code"] == "frigate_error"
    # Nothing to search by, both, blank words; no Frigate URL; Frigate detections off.
    for bad in ({}, {"query": "car", "bookmark_id": 21}, {"query": ""}):
        assert (await ask(hass, hass_ws_client, **bad))["error"]["code"] == "invalid_format"
    assert "nothing to search" in (await ask(hass, hass_ws_client, query="  "))["error"]["message"]
    bridge.api = None
    assert "Frigate's URL" in (await ask(hass, hass_ws_client, query="car"))["error"]["message"]
    assert await bridge.frigate_cameras([6]) is None
    del hass.data[DATA_FRIGATE][bridge.entry_id]
    assert "Frigate detections" in (await ask(hass, hass_ws_client, query="car"))["error"]["message"]


async def test_search_time_limit(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client) -> None:
    async def hang(*_args, **_kwargs):
        await asyncio.sleep(60)

    with patch.object(search_mod, "SEARCH_TIMEOUT_SECONDS", 0.05), patch.object(bridge.api, "json", hang):
        msg = await ask(hass, hass_ws_client, query="car")
    assert msg["error"] == {"code": "frigate_error", "message": "search: no answer in time (Frigate or Surveillance Station)"}


async def test_cameras_say_search(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/cameras"})
    assert (await ws.receive_json())["result"]["search"] is True


async def test_one_query_at_a_time(hass: HomeAssistant, bridge: FrigateBridge, hass_ws_client) -> None:
    """Frigate answers concurrent semantic searches with nothing: ours are asked one after another."""
    running = peak = 0

    async def frigate(path, params=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return [] if "search" in path else {"cameras": {"drive_way": {}}}

    with patch.object(bridge.api, "json", frigate):
        ws = await hass_ws_client(hass)
        for q in ("a", "b", "c"):
            await ws.send_json_auto_id({"type": "surveillance_station/search", "query": q})
        answers = [await ws.receive_json() for _ in range(3)]
    assert all(a["success"] for a in answers) and peak == 1
