"""Frigate review items become SS bookmarks, announced once per review."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_mqtt_message,
    flush_store,
)
from synology_ss_playback import Bookmark, Camera, SSConnectionError

from custom_components.surveillance_station.const import (
    CONF_FRIGATE,
    CONF_FRIGATE_OBJECTS,
    CONF_FRIGATE_LINK,
    CONF_FRIGATE_QUIET,
    CONF_FRIGATE_QUIET_KINDS,
    CONF_FRIGATE_TOPIC,
    DETECTION_EVENT,
    DOMAIN,
)
from custom_components.surveillance_station.frigate import DATA_FRIGATE, FrigateBridge, bookmark_comment, bookmark_name, camera_key
from custom_components.surveillance_station.views import DATA_MANAGER, VodManager
from homeassistant.core import HomeAssistant

T = 1_790_000_000


def review(kind: str, severity: str = "alert", objects=("person",), zones=(), end=None, rid="1790000000.1-abc", camera="drive_way", before=None, start=T + 0.4) -> dict[str, Any]:
    after = {
        "id": rid,
        "camera": camera,
        "start_time": start,
        "end_time": end,
        "severity": severity,
        "data": {"objects": list(objects), "zones": list(zones), "detections": ["x"], "thumb_time": start + 0.8},
    }
    return {"type": kind, "before": before or after, "after": after}


@pytest.fixture(autouse=True)
def clock():
    """Now is two seconds into the review T (a review minutes old is not announced)."""
    with patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 2) as now:
        yield now


@pytest.fixture
def client(mock_client: MagicMock) -> MagicMock:
    mock_client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True), Camera(id=10, name="Front Door", enabled=True)]
    ids = iter(range(100, 200))

    async def create(camera_id, name, start, end, comment=""):
        return Bookmark(id=next(ids), camera_id=camera_id, name=name, comment=comment, start=int(start), end=int(end))

    async def edit(bookmark_id, camera_id, name, start, end, comment=""):
        return Bookmark(id=bookmark_id, camera_id=camera_id, name=name, comment=comment, start=int(start), end=int(end))

    mock_client.create_bookmark = AsyncMock(side_effect=create)
    mock_client.edit_bookmark = AsyncMock(side_effect=edit)
    return mock_client


@pytest.fixture
def bridge(hass: HomeAssistant, setup_integration: MockConfigEntry, client: MagicMock) -> FrigateBridge:
    manager = hass.data[DATA_MANAGER]
    manager.thumbnail_when_recorded = AsyncMock(return_value=b"jpg")
    return FrigateBridge(
        hass, setup_integration.entry_id, client, manager, "frigate", {"person", "car", "dog", "cat"}, "/ss-playback/playback", 5, {"Animal"}
    )


def test_names() -> None:
    assert camera_key("drive_way") == camera_key("Drive Way") == "driveway"
    assert camera_key("BackyardPath") == camera_key("backyard_path")
    assert bookmark_name(["car", "person", "car", "license_plate"]) == "Car, Person, License plate"
    assert bookmark_name(["dog", "person", "cat", "bird"]) == "Animal, Person"
    assert bookmark_name([]) == "Detection"
    assert bookmark_comment("r1", "alert", ["front_yard", "porch"]) == "Frigate alert in front yard, porch [frigate r1]"


async def test_alert_lifecycle(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Created on new, renamed as objects are added, closed at the end; one event."""
    events = async_capture_events(hass, DETECTION_EVENT)
    clock = patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 2)
    with clock as now:
        await bridge.handle(review("new"))
        client.create_bookmark.assert_awaited_once_with(6, "Person", T, T + 30, "Frigate alert [frigate 1790000000.1-abc]")
        now.return_value = T + 40  # still going on: the bookmark reaches now
        await bridge.handle(review("update", objects=("person", "car"), zones=("porch",)))
        await bridge.handle(review("update", objects=("person", "car"), zones=("porch",)))  # nothing new
    assert client.edit_bookmark.await_count == 1
    assert client.edit_bookmark.await_args.args == (100, 6, "Person, Car", T, T + 40, "Frigate alert in porch [frigate 1790000000.1-abc]")
    await bridge.handle(review("end", objects=("person", "car"), zones=("porch",), end=T + 17.5))
    assert client.edit_bookmark.await_args.args[3:5] == (T, T + 18)
    assert not bridge._tracked
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert data["bookmark_id"] == 100 and data["camera_id"] == 6 and data["camera"] == "Drive Way"
    assert data["objects"] == ["Person"] and data["severity"] == "alert" and data["start"] == T
    assert data["url"] == f"/ss-playback/playback?ss_camera=6&ss_time={T - 3}"
    # The frame Frigate picked (T + 1.2, up to the next second), not the start:
    # large for the notification, the list's size too.
    assert data["image"].startswith(f"/api/surveillance_station/thumbnail/{bridge.entry_id}/6/{T + 2}-large.jpg?exp=")
    assert data["thumbnail"].startswith(f"/api/surveillance_station/thumbnail/{bridge.entry_id}/6/{T + 2}.jpg?exp=")
    assert bridge.manager.thumbnail_when_recorded.await_args.args[4] == 1280
    assert bridge.manager.frame(bridge.entry_id, Bookmark(100, 6, "", "", T, T + 18)) == T + 2
    bridge.manager.thumbnail_when_recorded.assert_awaited_once()


async def test_objects_not_severity(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """A cat detection counts (as an animal); a bicycle alone doesn't, until a person joins it."""
    events = async_capture_events(hass, DETECTION_EVENT)
    client.list_bookmarks.return_value = []
    await bridge.handle(review("new", severity="detection", objects=("cat",), rid="c1"))
    camera_id, name, _, _, comment = client.create_bookmark.await_args.args
    assert (camera_id, name, comment) == (6, "Animal", "Frigate detection [frigate c1]")
    await bridge.handle(review("new", objects=("bicycle",), rid="b1"))
    assert client.create_bookmark.await_count == 1
    await bridge.handle(review("update", objects=("bicycle", "person", "dog"), rid="b1"))
    assert client.create_bookmark.await_args.args[1] == "Person, Animal"
    await hass.async_block_till_done()
    assert [(e.data["review_id"], e.data["objects"], e.data["labels"]) for e in events] == [
        ("c1", ["Animal"], ["cat"]),
        ("b1", ["Person", "Animal"], ["person", "dog"]),
    ]


async def test_review_after_a_restart_finds_its_bookmark(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Its comment names the review: the end goes to that bookmark, nothing new is made or announced."""
    events = async_capture_events(hass, DETECTION_EVENT)
    client.list_bookmarks.return_value = [
        Bookmark(id=7, camera_id=6, name="Person", comment="Frigate alert [frigate 1790000000.1-abc]", start=T, end=T + 30)
    ]
    await bridge.handle(review("end", end=T + 50))
    client.create_bookmark.assert_not_awaited()
    assert client.edit_bookmark.await_args.args[:5] == (7, 6, "Person", T, T + 51)
    await hass.async_block_till_done()
    assert not events


async def test_review_heard_of_only_at_its_end(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Bookmarked (HA was down when it began), but not announced: old news."""
    events = async_capture_events(hass, DETECTION_EVENT)
    client.list_bookmarks.return_value = []
    await bridge.handle(review("end", end=T + 9))
    assert client.create_bookmark.await_args.args[2:4] == (T, T + 10)
    await hass.async_block_till_done()
    assert not events


async def test_ignored(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    await bridge.handle(review("new", camera="garage"))  # no such SS camera
    await bridge.handle(review("new", camera="garage"))
    await bridge.handle(review("genai"))
    await bridge.handle(review("new", rid="bad id/../x"))
    await bridge.handle(review("new", objects=("bicycle",)))
    client.create_bookmark.assert_not_awaited()
    assert client.cameras.await_count == 1  # listed again at most once a minute


async def test_no_link(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    events = async_capture_events(hass, DETECTION_EVENT)
    bridge.link = ""
    await bridge.handle(review("new"))
    await hass.async_block_till_done()
    assert events[0].data["url"] is None


async def test_bookmarks_listed_afresh(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """The card sees a new bookmark at once, not after the list cache expires."""
    manager = bridge.manager
    client.list_bookmarks.return_value = []
    assert await manager.bookmarks(bridge.entry_id, client) == []
    await bridge.handle(review("new"))
    client.list_bookmarks.return_value = [Bookmark(id=100, camera_id=6, name="Person", comment="", start=T, end=T + 30)]
    assert [b.id for b in await manager.bookmarks(bridge.entry_id, client)] == [100]


@pytest.mark.parametrize("expected_lingering_timers", [True])  # MQTT's own housekeeping
async def test_over_mqtt(hass: HomeAssistant, mqtt_mock, mock_config_entry: MockConfigEntry, client: MagicMock) -> None:
    """Options on: subscribed to <topic>/reviews; SS errors are logged, not fatal."""
    events = async_capture_events(hass, DETECTION_EVENT)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_config_entry.data,
        unique_id=mock_config_entry.unique_id,
        options={CONF_FRIGATE: True, CONF_FRIGATE_TOPIC: "nvr", CONF_FRIGATE_OBJECTS: ["person"], CONF_FRIGATE_LINK: ""},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.surveillance_station.views.VodManager.thumbnail_when_recorded", AsyncMock(return_value=None)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        client.create_bookmark.side_effect = SSConnectionError("x", "Create", None)
        async_fire_mqtt_message(hass, "nvr/reviews", json.dumps(review("new", rid="a1")))
        async_fire_mqtt_message(hass, "nvr/reviews", "not json")
        async_fire_mqtt_message(hass, "frigate/reviews", json.dumps(review("new", rid="a2")))
        await hass.async_block_till_done()
        assert client.create_bookmark.await_count == 1
        client.create_bookmark.side_effect = None
        client.create_bookmark.return_value = Bookmark(id=5, camera_id=6, name="Person", comment="", start=T, end=T + 30)
        async_fire_mqtt_message(hass, "nvr/reviews", json.dumps(review("new", rid="a3")))
        await hass.async_block_till_done()
        assert client.create_bookmark.await_count == 2
        assert [e.data["review_id"] for e in events] == ["a3"]
        stats = hass.data[DATA_FRIGATE][entry.entry_id].stats()
        assert stats["subscribed"] and stats["topic"] == "nvr/reviews"
        # "not json" and the other topic never count; a1 failed, a3 made it.
        assert {k: stats[k] for k in ("received", "bookmarked", "announced", "failed", "dropped", "queued")} == {
            "received": 2, "bookmarked": 1, "announced": 1, "failed": 1, "dropped": 0, "queued": 0,
        }
        assert "Create" in stats["last_error"]["error"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    flow = await hass.config_entries.options.async_init(setup_integration.entry_id)
    base = {CONF_FRIGATE: True, CONF_FRIGATE_OBJECTS: ["person", " Dog "]}
    for bad, field, error in (
        ({CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_OBJECTS: [" "]}, CONF_FRIGATE_OBJECTS, "no_objects"),
        ({CONF_FRIGATE_TOPIC: "frigate/#"}, CONF_FRIGATE_TOPIC, "invalid_topic"),
        ({CONF_FRIGATE_TOPIC: " / "}, CONF_FRIGATE_TOPIC, "invalid_topic"),
        ({CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_LINK: "ss-playback"}, CONF_FRIGATE_LINK, "invalid_link"),
    ):
        result = await hass.config_entries.options.async_configure(flow["flow_id"], {**base, **bad})
        assert result["errors"] == {field: error}
    with patch("custom_components.surveillance_station.FrigateBridge.start", AsyncMock(return_value=None)) as start:
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"], {**base, CONF_FRIGATE_TOPIC: " frigate/ ", CONF_FRIGATE_LINK: " /ss-playback/playback "}
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert setup_integration.options == {
        CONF_FRIGATE: True,
        CONF_FRIGATE_OBJECTS: ["dog", "person"],
        CONF_FRIGATE_TOPIC: "frigate",
        CONF_FRIGATE_LINK: "/ss-playback/playback",
        CONF_FRIGATE_QUIET: 5,
        CONF_FRIGATE_QUIET_KINDS: ["Animal"],
    }
    start.assert_awaited_once()  # reloaded with the bridge on


async def test_better_frame_later(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Frigate picks a better frame as the review goes on: the event list's thumbnail follows it, and survives a restart."""
    manager = bridge.manager
    r = review("new")
    await bridge.handle(r)
    r["after"]["data"]["thumb_time"] = T + 6.5
    r["type"] = "update"
    await bridge.handle(r)
    client.edit_bookmark.assert_not_awaited()  # nothing else changed
    bm = Bookmark(100, 6, "Person", "", T, T + 30)
    assert manager.frame(bridge.entry_id, bm) == T + 7
    assert manager.frame(bridge.entry_id, Bookmark(99, 6, "", "", T, T)) == T + 1  # not Frigate's: a second in
    await flush_store(manager._frame_store)
    again = VodManager(hass)
    await again.async_load()
    assert again.frame(bridge.entry_id, bm) == T + 7


async def test_verified_and_order(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """A recognised person is still a person; names don't flip with Frigate's set order."""
    await bridge.handle(review("new", objects=("car", "person-verified")))
    assert client.create_bookmark.await_args.args[1] == "Person, Car"
    await bridge.handle(review("update", objects=("person-verified", "car")))
    client.edit_bookmark.assert_not_awaited()


async def test_old_news_not_announced(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Bookmarked minutes late (SS was down, HA restarted mid-review): no notification."""
    events = async_capture_events(hass, DETECTION_EVENT)
    with patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 200):
        await bridge.handle(review("new"))
    client.create_bookmark.assert_awaited_once()
    await hass.async_block_till_done()
    assert not events


async def test_unloading_doesnt_announce(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Stopped while waiting for SS to record the frame: no event with a frame not there yet."""
    events = async_capture_events(hass, DETECTION_EVENT)
    waiting = asyncio.Event()

    async def wait(*args):
        waiting.set()
        await asyncio.sleep(3600)

    bridge.manager.thumbnail_when_recorded = wait
    await bridge.handle(review("new"))
    async with asyncio.timeout(5):
        await waiting.wait()
    bridge.stop()
    await hass.async_block_till_done()
    assert not events


async def test_malformed_reviews(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    r = review("new")
    del r["after"]["start_time"]
    await bridge.handle(r)
    r["after"]["start_time"] = None
    await bridge.handle(r)
    client.create_bookmark.assert_not_awaited()


async def test_quiet_period(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, clock) -> None:
    """The dog that wandered off and came back: bookmarked again, not announced again, unless a new kind shows up."""
    events = async_capture_events(hass, DETECTION_EVENT)
    client.list_bookmarks.return_value = []
    await bridge.handle(review("new", objects=("dog",), rid="d1", camera="drive_way"))
    clock.return_value = T + 40
    await bridge.handle(review("end", objects=("dog",), rid="d1", end=T + 38))
    clock.return_value = T + 200  # 162 s after the dog was last seen
    await bridge.handle(review("new", objects=("cat",), rid="d2", start=T + 199))  # an animal again
    await bridge.handle(review("new", objects=("cat", "person"), rid="d3", start=T + 199))  # a person is news
    await bridge.handle(review("new", objects=("dog",), rid="d4", camera="Front Door", start=T + 199))  # another camera
    clock.return_value = T + 200 + 5 * 60 + 1  # 5 min after the last animal here (the cat, d2)
    await bridge.handle(review("new", objects=("dog",), rid="d5", start=T + 500))  # quiet period over
    await hass.async_block_till_done()
    assert client.create_bookmark.await_count == 5
    assert [e.data["review_id"] for e in events] == ["d1", "d3", "d4", "d5"]


async def test_quiet_period_off(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    events = async_capture_events(hass, DETECTION_EVENT)
    bridge.quiet = 0
    await bridge.handle(review("new", rid="p1"))
    await bridge.handle(review("new", rid="p2"))
    await hass.async_block_till_done()
    assert len(events) == 2


async def test_people_are_never_quiet(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, clock) -> None:
    """A second person a minute after the first is announced, even if people are asked to be quiet; cars only when asked."""
    events = async_capture_events(hass, DETECTION_EVENT)
    bridge2 = FrigateBridge(
        hass, bridge.entry_id, client, bridge.manager, "frigate", {"person", "car"}, "", 5, {"Person", "Car"}
    )
    assert bridge2.quiet_kinds == {"Car"}
    for b in (bridge, bridge2):
        for i, what in enumerate(("person", "person", "car", "car")):
            clock.return_value = T + 60 * i + 1
            await b.handle(review("new", objects=(what,), rid=f"{id(b)}-{i}", start=T + 60 * i))
    await hass.async_block_till_done()
    # bridge: cars aren't quiet by default; bridge2: the second car is.
    assert [e.data["objects"] for e in events] == [["Person"]] * 2 + [["Car"]] * 2 + [["Person"]] * 2 + [["Car"]]
