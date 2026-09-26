"""Frigate review items become SS bookmarks, announced once per review."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_mqtt_message,
    async_fire_time_changed,
    flush_store,
)
from synology_ss_playback import Bookmark, Camera, SSAuthError, SSConnectionError, SSError

from custom_components.surveillance_station.const import (
    CONF_FRIGATE,
    CONF_FRIGATE_CAMERAS,
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
from homeassistant.components import mqtt as mqtt_mod
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

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
    assert camera_key("前门 (外)") == "前门外" != camera_key("后院")  # any script
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
    bridge._decide("1790000000.1-abc")  # announced before the restart (remembered)
    client.list_bookmarks.return_value = [
        Bookmark(id=7, camera_id=6, name="Person", comment="Frigate alert [frigate 1790000000.1-abc]", start=T, end=T + 30)
    ]
    await bridge.handle(review("end", end=T + 50))
    client.create_bookmark.assert_not_awaited()
    assert client.edit_bookmark.await_args.args[:5] == (7, 6, "Person", T, T + 51)
    await hass.async_block_till_done()
    assert not events


async def test_review_heard_of_only_at_its_end(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Its first messages were lost (SS failed, HA restarting): bookmarked at its end, announced if recent."""
    events = async_capture_events(hass, DETECTION_EVENT)
    client.list_bookmarks.return_value = []
    await bridge.handle(review("end", end=T + 9))
    assert client.create_bookmark.await_args.args[2:4] == (T, T + 10)
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_found_after_a_restart_undecided_is_announced_once(
    hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock
) -> None:
    """Bookmarked just before a restart, notification not yet sent: sent now, and only once."""
    events = async_capture_events(hass, DETECTION_EVENT)
    client.list_bookmarks.return_value = [
        Bookmark(id=7, camera_id=6, name="Person", comment="Frigate alert [frigate 1790000000.1-abc]", start=T, end=T + 30)
    ]
    await bridge.handle(review("update"))
    await bridge.handle(review("update", zones=("porch",)))
    await hass.async_block_till_done()
    client.create_bookmark.assert_not_awaited()
    assert [e.data["bookmark_id"] for e in events] == [7]


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
async def test_over_mqtt(hass: HomeAssistant, mqtt_mock, mock_config_entry: MockConfigEntry, client: MagicMock, caplog) -> None:
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
    ), patch("custom_components.surveillance_station.frigate.FRIGATE_RETRY_SECONDS", 0):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        client.create_bookmark.side_effect = SSConnectionError("x", "Create", None)
        async_fire_mqtt_message(hass, "nvr/reviews", json.dumps(review("new", rid="a1")))
        async_fire_mqtt_message(hass, "nvr/reviews", "not json")
        async_fire_mqtt_message(hass, "nvr/reviews", json.dumps({"type": "new"}))  # no "after"
        async_fire_mqtt_message(hass, "frigate/reviews", json.dumps(review("new", rid="a2")))
        bridge = hass.data[DATA_FRIGATE][entry.entry_id]
        # The worker is a background task (block_till_done doesn't wait for it).
        async with asyncio.timeout(5):
            while bridge._pending or bridge._counts["failed"] < 1:
                await asyncio.sleep(0.01)
        assert client.create_bookmark.await_count == 3  # tried, then retried twice
        client.create_bookmark.side_effect = None
        client.create_bookmark.return_value = Bookmark(id=5, camera_id=6, name="Person", comment="", start=T, end=T + 30)
        async_fire_mqtt_message(hass, "nvr/reviews", json.dumps(review("new", rid="a3")))
        async with asyncio.timeout(5):
            while bridge._counts["announced"] < 1:
                await asyncio.sleep(0.01)
        async with asyncio.timeout(5):
            while bridge._counts["announced"] < 2:
                await asyncio.sleep(0.01)
        # a3 went through: SS answers, so a1 (deferred) is tried again too.
        assert client.create_bookmark.await_count == 5
        assert [e.data["review_id"] for e in events] == ["a3", "a1"]
        stats = hass.data[DATA_FRIGATE][entry.entry_id].stats()
        assert stats["subscribed"] and stats["topic"] == "nvr/reviews"
        # Not JSON / no "after": not a review message. a1 failed, a3 made it.
        assert {k: stats[k] for k in ("messages", "retried", "bookmarked", "announced", "failed", "deferred", "queued")} == {
            "messages": 2, "retried": 2, "bookmarked": 2, "announced": 2, "failed": 1, "deferred": 0, "queued": 0,
        }
        assert not stats["failing"]  # a3 went through after a1's failure
        assert stats["last_error"]["error"].startswith("x.Create failed")
        assert stats["last_error"]["at"].endswith("+00:00") and stats["last_message"]
    # Its state not saved (disk full...): logged, the unload still goes through.
    with patch.object(bridge, "async_flush", AsyncMock(side_effect=OSError("disk full"))):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert "Could not save the Frigate bridge's state" in caplog.text


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
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**base, CONF_FRIGATE_TOPIC: " frigate/ ", CONF_FRIGATE_LINK: " /ss-playback/playback "}
    )
    # Then SS's cameras (as SS lists them, sorted), for names that don't match Frigate's.
    assert result["step_id"] == "cameras"
    assert [str(k) for k in result["data_schema"].schema] == ["Backyard", "Drive Way"]
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"Backyard": "back_yard", "Drive Way": "Back Yard"}
    )
    assert result["errors"] == {"base": "duplicate_camera"}
    with patch("custom_components.surveillance_station.FrigateBridge.start", AsyncMock(return_value=None)) as start:
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"Backyard": " back_yard , garden, Back_Yard,", "Drive Way": " "}  # a name twice: once, in place
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
        CONF_FRIGATE_CAMERAS: {"Backyard": "back_yard, garden"},
    }
    start.assert_awaited_once()  # reloaded with the bridge on
    assert hass.data[DATA_FRIGATE][setup_integration.entry_id]._aliases == {"backyard": "Backyard", "garden": "Backyard"}


@pytest.mark.parametrize("case", ["frigate_off", "ss_down", "not_loaded"])
async def test_options_without_the_camera_step(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, case: str
) -> None:
    """Frigate off, SS not answering, or the entry not loaded: saved at once, the mapping kept as it was."""
    hass.config_entries.async_update_entry(setup_integration, options={CONF_FRIGATE_CAMERAS: {"Backyard": "garden"}})
    await hass.async_block_till_done()
    frigate = case != "frigate_off"
    if case == "ss_down":
        mock_client.cameras.side_effect = SSConnectionError("x", "List", None)
    elif case == "not_loaded":
        assert await hass.config_entries.async_unload(setup_integration.entry_id)
        if hasattr(setup_integration, "runtime_data"):
            del setup_integration.runtime_data
    before = mock_client.cameras.await_count
    flow = await hass.config_entries.options.async_init(setup_integration.entry_id)
    with patch("custom_components.surveillance_station.FrigateBridge.start", AsyncMock(return_value=None)):
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"], {CONF_FRIGATE: frigate, CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_OBJECTS: ["person"]}
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert setup_integration.options[CONF_FRIGATE_CAMERAS] == {"Backyard": "garden"}
    assert mock_client.cameras.await_count - before == (1 if case == "ss_down" else 0)  # asked only when it can answer


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


async def test_stats_ignored_and_dropped(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Silence with messages arriving says why: which messages were ignored, or dropped."""
    await bridge.handle({"type": "genai", "after": {"id": "x"}})
    await bridge.handle({"type": "new", "after": {"id": "x"}})  # no start_time
    await bridge.handle(review("new", objects=("bicycle",), rid="b1"))
    await bridge.handle(review("new", camera="garage", rid="b2"))
    assert bridge.stats()["ignored"] == {"other_type": 1, "malformed": 1, "no_objects": 1, "unknown_camera": 1}
    client.create_bookmark.assert_not_called()

    # No worker on this bridge: nothing drains what is waiting.
    with patch("custom_components.surveillance_station.frigate.FRIGATE_QUEUE_MAX", 2):
        for rid in ("q1", "q2", "q1", "q3"):
            bridge._received(MagicMock(payload=json.dumps(review("update", rid=rid))))
    stats = bridge.stats()
    # q1's second message replaced its first; q3 pushed the oldest (q1) out,
    # to wait with those SS failed (not lost).
    assert (stats["messages"], stats["coalesced"], stats["dropped"], stats["queued"], stats["deferred"]) == (4, 1, 0, 2, 1)
    assert list(bridge._pending) == ["q2", "q3"]


async def test_announced_even_if_the_frame_fails(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    events = async_capture_events(hass, DETECTION_EVENT)
    with patch.object(bridge.manager, "thumbnail_when_recorded", AsyncMock(side_effect=OSError("ffmpeg gone"))):
        await bridge.handle(review("new"))
        await hass.async_block_till_done()
    assert len(events) == 1
    stats = bridge.stats()
    assert (stats["announced"], stats["announce_failed"], stats["announcing"]) == (1, 1, 0)
    assert stats["last_error"]["error"] == "OSError: ffmpeg gone"


async def test_not_announced_counted(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    with patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 600):
        await bridge.handle(review("end", end=T + 20))  # heard of only at its end, long after
    assert (bridge.stats()["bookmarked"], bridge.stats()["not_announced"]) == (1, 1)


# --- reliability -----------------------------------------------------------


@pytest.fixture
def no_retry_wait():
    with patch("custom_components.surveillance_station.frigate.FRIGATE_RETRY_SECONDS", 0):
        yield


async def test_transient_error_is_retried(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait) -> None:
    """One SS hiccup on a review's first message: retried, bookmarked and announced as normal."""
    events = async_capture_events(hass, DETECTION_EVENT)
    create = client.create_bookmark.side_effect
    calls = []

    async def flaky(*args):
        calls.append(args)
        if len(calls) == 1:
            raise SSConnectionError("x", "Create", None)
        return await create(*args)

    client.create_bookmark.side_effect = flaky
    await bridge._process("1790000000.1-abc", review("new"))
    await hass.async_block_till_done()
    assert len(calls) == 2
    assert len(events) == 1
    stats = bridge.stats()
    assert (stats["retried"], stats["failed"], stats["failing"]) == (1, 0, False)


async def test_retry_takes_the_reviews_newer_message(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait) -> None:
    create = client.create_bookmark.side_effect
    calls = []

    async def flaky(*args):
        calls.append(args)
        if len(calls) == 1:
            # Meanwhile the review went on: its newer message waits.
            bridge._pending["1790000000.1-abc"] = review("update", objects=("person", "car"))
            raise SSConnectionError("x", "Create", None)
        return await create(*args)

    client.create_bookmark.side_effect = flaky
    await bridge._process("1790000000.1-abc", review("new"))
    assert calls[1][1] == "Person, Car"
    assert not bridge._pending


async def test_no_retry_while_failing_or_refused(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait) -> None:
    """SS known down: fail fast (no queue stalled behind timeouts). Refused credentials: never."""
    down = SSConnectionError("x", "Create", None)
    client.create_bookmark.side_effect = down
    client.list_bookmarks.side_effect = down
    await bridge._process("a", review("new", rid="a"))
    # Tried, then twice more (each looking for a bookmark the failed try may have made).
    assert (client.create_bookmark.await_count, client.list_bookmarks.await_count) == (1, 2)
    await bridge._process("b", review("new", rid="b"))
    assert client.create_bookmark.await_count == 2
    assert list(bridge._deferred) == ["a", "b"]
    client.create_bookmark.reset_mock()
    bridge._failing_since = None
    client.create_bookmark.side_effect = SSAuthError("SYNO.API.Auth", "login", 400)
    await bridge._process("c", review("new", rid="c"))
    assert client.create_bookmark.await_count == 1


async def test_camera_list_reread_after_a_failure(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait) -> None:
    """A camera replaced in SS (same name, new id): the next try looks it up again."""
    create = client.create_bookmark.side_effect
    client.create_bookmark.side_effect = [SSError("x", "Create", 400), SSError("x", "Create", 400), SSError("x", "Create", 400)]
    await bridge._process("a", review("new", rid="a"))
    client.cameras.return_value = [Camera(id=16, name="Drive Way", enabled=True)]
    client.create_bookmark.side_effect = create
    await bridge._process("b", review("new", rid="b"))
    assert client.create_bookmark.await_args.args[0] == 16


async def test_camera_list_reread_regularly(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    await bridge.handle(review("new", rid="a"))
    with patch("custom_components.surveillance_station.frigate._monotonic", return_value=time.monotonic() + 601):
        await bridge.handle(review("new", rid="b"))
    assert client.cameras.await_count == 2


async def test_waits_for_mqtt(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """MQTT not there yet (starting, reloading): keeps waiting instead of giving up."""
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(side_effect=[False, False, True])) as wait, patch(
        "custom_components.surveillance_station.frigate.FRIGATE_MQTT_RETRY_SECONDS", 0
    ), patch.object(mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())) as subscribe:
        assert await bridge.start()
    assert wait.await_count == 3
    assert [c.args[1] for c in subscribe.await_args_list] == ["frigate/reviews", "frigate/available"]
    assert bridge.stats()["subscribed"]
    bridge.stop()
    assert not bridge.stats()["subscribed"]


async def test_nothing_after_stop(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """A subscription HA's MQTT carried over to a new client after unsubscribing: ignored."""
    bridge.stop()
    bridge._received(MagicMock(payload=json.dumps(review("new"))))
    assert bridge.stats()["messages"] == 0 and not bridge._pending


async def test_state_survives_a_restart(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, hass_storage) -> None:
    """Announced reviews and the quiet period are remembered: no second notification after a restart."""
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("person",), rid="p1"))
    await bridge.handle(review("new", objects=("dog",), rid="d1"))
    await hass.async_block_till_done()
    assert len(events) == 2
    await flush_store(bridge._store)

    again = FrigateBridge(
        hass, bridge.entry_id, client, bridge.manager, "frigate", {"person", "car", "dog", "cat"}, "/p", 5, {"Animal"}
    )
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ):
        await again.start()
    assert {"p1", "d1"} <= set(again._decided)
    client.list_bookmarks.return_value = [
        Bookmark(id=100, camera_id=6, name="Person", comment="Frigate alert [frigate p1]", start=T, end=T + 30)
    ]
    await again.handle(review("update", objects=("person",), rid="p1"))  # the same review goes on
    await again.handle(review("new", objects=("cat",), rid="d2"))  # another animal, within the quiet period
    await hass.async_block_till_done()
    assert len(events) == 2
    again.stop()


async def test_announce_is_bounded(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """SS or ffmpeg hanging on the frame: the notification goes anyway."""
    events = async_capture_events(hass, DETECTION_EVENT)

    async def hang(*args):
        await asyncio.Event().wait()

    with patch.object(bridge.manager, "thumbnail_when_recorded", AsyncMock(side_effect=hang)), patch(
        "custom_components.surveillance_station.frigate.FRIGATE_EVENT_WAIT_SECONDS", -9.95
    ):
        await bridge.handle(review("new"))
        await asyncio.wait_for(asyncio.gather(*bridge._announcing), 5)
    assert len(events) == 1
    assert bridge.stats()["announce_failed"] == 1


async def test_lasting_problems_become_repairs_issues(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    registry = ir.async_get(hass)
    client.cameras.side_effect = SSConnectionError("x", "List", None)  # the probe finds SS still down
    issue = f"frigate_failing_{bridge.entry_id}"
    bridge._subscribed = True
    bridge._failing_since = time.monotonic() - 30
    bridge._check_health()
    assert registry.async_get_issue(DOMAIN, issue) is None  # not for long yet
    bridge._failing_since = time.monotonic() - 601
    bridge._check_health()
    assert registry.async_get_issue(DOMAIN, issue) is not None
    bridge._failing_since = None  # a bookmark went through
    bridge._check_health()
    assert registry.async_get_issue(DOMAIN, issue) is None

    bridge._availability(MagicMock(payload="offline"))
    bridge._frigate_offline_since -= 601
    bridge._check_health()
    assert registry.async_get_issue(DOMAIN, f"frigate_offline_{bridge.entry_id}") is not None
    assert bridge.stats()["frigate_available"] == "offline"
    bridge.stop()  # unloaded: its issues go with it
    assert registry.async_get_issue(DOMAIN, f"frigate_offline_{bridge.entry_id}") is None

    fresh = FrigateBridge(hass, bridge.entry_id, bridge.client, bridge.manager, "frigate", {"person"}, "")
    fresh._started_at -= 601  # never got MQTT
    fresh._check_health()
    assert registry.async_get_issue(DOMAIN, f"frigate_mqtt_{bridge.entry_id}") is not None
    fresh.stop()



async def test_seen_new_survives_coalescing(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """A review's "new" replaced by its "update" while waiting: still known new (no bookmark search)."""
    bridge._received(MagicMock(payload=json.dumps(review("new"))))
    bridge._received(MagicMock(payload=json.dumps(review("update", objects=("person", "car")))))
    key, message = bridge._pending.popitem(last=False)
    await bridge._process(key, message)
    client.list_bookmarks.assert_not_called()
    assert client.create_bookmark.await_args.args[1] == "Person, Car"


async def test_unexpected_error_is_not_retried_but_survived(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    client.create_bookmark.side_effect = RuntimeError("bug")
    await bridge._process("a", review("new", rid="a"))
    assert client.create_bookmark.await_count == 1
    stats = bridge.stats()
    assert stats["failed"] == 1 and stats["last_error"]["error"] == "RuntimeError: bug"
    assert not stats["failing"] and not stats["deferred"]  # a bug in one message, not SS down


async def test_frigate_back_online(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    bridge._availability(MagicMock(payload="offline"))
    bridge._availability(MagicMock(payload="online"))
    assert bridge._frigate_offline_since is None and bridge.stats()["frigate_available"] == "online"


async def test_unreadable_state_is_ignored(hass: HomeAssistant, bridge: FrigateBridge, hass_storage, caplog) -> None:
    hass_storage[bridge._store.key] = {"version": 1, "key": bridge._store.key, "data": {"last_seen": [["x", "Animal", T]]}}
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ):
        assert await bridge.start()
    assert "unreadable Frigate state" in caplog.text
    bridge.stop()



async def test_retry_finds_a_bookmark_made_before_the_error(
    hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait
) -> None:
    """SS made it, only its answer was lost: the retry finds it rather than make a second one."""
    made = []

    async def made_then_lost(camera_id, name, start, end, comment=""):
        made.append(Bookmark(id=100, camera_id=camera_id, name=name, comment=comment, start=int(start), end=int(end)))
        raise SSConnectionError("x", "Create", None)

    client.create_bookmark.side_effect = made_then_lost
    client.list_bookmarks.side_effect = lambda ids: list(made)
    await bridge._process("1790000000.1-abc", review("new"))
    assert len(made) == 1
    assert bridge._tracked["1790000000.1-abc"].bookmark_id == 100


async def test_cut_short_announcement_is_tried_again(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Unloaded (or restarting) while waiting for the frame: not counted as announced."""
    events = async_capture_events(hass, DETECTION_EVENT)
    waiting = asyncio.Event()

    async def wait(*args):
        waiting.set()
        await asyncio.Event().wait()

    with patch.object(bridge.manager, "thumbnail_when_recorded", AsyncMock(side_effect=wait)):
        await bridge.handle(review("new"))
        await bridge.handle(review("update"))  # while it waits: no second announcement
        await waiting.wait()
        for task in list(bridge._announcing):
            task.cancel()
        await asyncio.gather(*bridge._announcing, return_exceptions=True)
    assert not events and "1790000000.1-abc" not in bridge._decided
    await bridge.handle(review("update", zones=("porch",)))
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_unload_writes_state_and_removal_deletes_it(
    hass: HomeAssistant, mqtt_mock, mock_config_entry: MockConfigEntry, client: MagicMock, hass_storage
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_config_entry.data, unique_id=mock_config_entry.unique_id, options={CONF_FRIGATE: True}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bridge = hass.data[DATA_FRIGATE][entry.entry_id]
    bridge._decide("r1")
    bridge._save()  # delayed: not written yet
    key = f"{DOMAIN}.frigate.{entry.entry_id}"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[key]["data"]["decided"] == ["r1"]  # written on unload, not 2 s later
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()
    assert key not in hass_storage  # and not written back by a pending save


async def test_deferred_reviews_go_through_once_ss_answers(
    hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait
) -> None:
    """Failed while SS was down: the oldest is tried again every minute; once it goes through, all the others follow."""
    create = client.create_bookmark.side_effect
    client.create_bookmark.side_effect = SSConnectionError("x", "Create", None)
    client.list_bookmarks.return_value = []
    for rid in ("a", "b", "c"):
        await bridge._process(rid, review("new", rid=rid))
    assert bridge.stats()["failing"] and list(bridge._deferred) == ["a", "b", "c"]
    # A message that doesn't reach SS, or reads that work, say nothing about bookmarks.
    await bridge._process("x", review("genai", rid="x"))
    assert bridge.stats()["failing"]

    bridge._subscribed = True
    bridge._check_health()  # still down: "a" tried, deferred again (now last)
    await bridge._process("a", bridge._pending.pop("a"))
    assert list(bridge._deferred) == ["b", "c", "a"] and not bridge._pending

    client.create_bookmark.side_effect = create
    bridge._check_health()
    assert list(bridge._pending) == ["b"]
    await bridge._process("b", bridge._pending.pop("b"))
    assert not bridge.stats()["failing"]
    # The rest replayed, after anything fresh.
    assert list(bridge._replay) == ["c", "a"] and not bridge._deferred and not bridge._pending


async def test_failing_with_nothing_waiting_is_probed(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    bridge._failing_since = time.monotonic()
    bridge._subscribed = True
    bridge._check_health()
    await bridge._probe
    assert not bridge.stats()["failing"]


async def test_failing_probe_keeps_the_error(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    bridge._failing_since = time.monotonic()
    bridge._subscribed = True
    client.cameras.side_effect = SSConnectionError("SYNO.SurveillanceStation.Camera", "List", None, "TimeoutError")
    bridge._check_health()
    await bridge._probe
    assert bridge.stats()["failing"] and "TimeoutError" in bridge._bookmark_error


async def test_subscribe_failure_is_retried(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    first = MagicMock()
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch(
        "custom_components.surveillance_station.frigate.FRIGATE_MQTT_RETRY_SECONDS", 0
    ), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(side_effect=[first, HomeAssistantError("gone"), MagicMock(), MagicMock()])
    ):
        assert await bridge.start()
    first.assert_called_once()  # the half-made subscription undone
    assert bridge.stats()["subscribed"]
    bridge.stop()


async def test_waiting_retry_keeps_maybe_made_when_its_review_goes_on(
    hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock
) -> None:
    """A deferred try (bookmark maybe made) replaced by the review's next message: still looked for first."""
    bridge._defer("r", {**review("new", rid="r"), "_maybe_made": True})
    bridge._received(MagicMock(payload=json.dumps(review("update", rid="r"))))
    assert not bridge._deferred
    client.list_bookmarks.return_value = [
        Bookmark(id=100, camera_id=6, name="Person", comment="Frigate alert [frigate r]", start=T, end=T + 30)
    ]
    await bridge._process("r", bridge._pending.pop("r"))
    client.create_bookmark.assert_not_awaited()


async def test_replay_stops_when_ss_fails_again(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait) -> None:
    """SS down again mid-replay: the rest wait again, without a request each."""
    for rid in ("a", "b", "c"):
        bridge._replay[rid] = review("new", rid=rid)
    client.create_bookmark.side_effect = SSConnectionError("x", "Create", None)
    client.list_bookmarks.side_effect = SSConnectionError("x", "List", None)
    worker = asyncio.create_task(bridge._work())
    bridge._wake.set()
    async with asyncio.timeout(5):
        while len(bridge._deferred) < 3:
            await asyncio.sleep(0.01)
    worker.cancel()
    assert client.create_bookmark.await_count == 1  # "a" (tried, retried twice: once created, then looked for)
    assert not bridge._replay


async def test_ss_saying_no_is_not_retried_forever(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait) -> None:
    """An error code for this request (a camera disabled in SS...): logged, not kept for later."""
    client.create_bookmark.side_effect = SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", "Create", 400)
    await bridge._process("r", review("new", rid="r"))
    assert client.create_bookmark.await_count == 1
    stats = bridge.stats()
    assert (stats["rejected"], stats["deferred"]) == (1, 0)


async def test_quiet_kind_cut_short_is_still_announced(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """A dog's notification cut short: its own sighting doesn't count as "seen lately" against it."""
    events = async_capture_events(hass, DETECTION_EVENT)

    async def wait(*args):
        await asyncio.Event().wait()

    with patch.object(bridge.manager, "thumbnail_when_recorded", AsyncMock(side_effect=wait)):
        await bridge.handle(review("new", objects=("dog",), rid="d"))
        await asyncio.sleep(0)
        for task in list(bridge._announcing):
            task.cancel()
        await asyncio.gather(*bridge._announcing, return_exceptions=True)
    await bridge.handle(review("update", objects=("dog",), rid="d"))
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_old_replay_doesnt_silence_the_present(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """An hour-old dog replayed after an outage: a dog now is still news."""
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("end", objects=("dog",), rid="old", start=T - 3600, end=T - 3500))
    await bridge.handle(review("new", objects=("dog",), rid="now"))
    await hass.async_block_till_done()
    assert [e.data["review_id"] for e in events] == ["now"]


async def test_waiting_reviews_survive_a_restart(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, hass_storage) -> None:
    """SS down, then HA restarts: what waited is tried again at start (bookmarked, not stale-announced)."""
    bridge._defer("r", {**review("new", rid="r", start=T - 900), "_maybe_made": True})
    bridge._pending["p"] = review("new", rid="p")  # not handled yet when stopped
    bridge.stop()
    await bridge.async_flush()

    again = FrigateBridge(hass, bridge.entry_id, client, bridge.manager, "frigate", {"person"}, "")
    client.list_bookmarks.return_value = []
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ):
        await again.start()
    async with asyncio.timeout(5):
        while client.create_bookmark.await_count < 2:
            await asyncio.sleep(0.01)
    assert {c.args[4].split("[frigate ")[1] for c in client.create_bookmark.await_args_list} == {"r]", "p]"}
    again.stop()


async def test_given_up_after_a_day(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    bridge._defer("r", review("new", rid="r"))
    bridge._deferred["r"]["_failed_at"] = T - 86401
    bridge._check_health()
    assert not bridge._deferred and bridge.stats()["dropped"] == 1


async def test_older_message_failing_defers_nothing_when_a_newer_waits(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """A review's "new" fails while its "update" waits: the update goes instead, knowing a bookmark may exist.

    Deferring the "new" as well would replay it after the update was
    bookmarked, and make a second bookmark."""
    for queue in (bridge._pending, bridge._replay):
        queue["r"] = review("update", rid="r")
        bridge._defer("r", review("new", rid="r"))
        assert not bridge._deferred and queue["r"]["_maybe_made"] and queue["r"]["type"] == "update"
        queue.clear()


async def test_stopped_before_ss_answered_still_expires(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """Kept on stop without ever reaching SS: dated, so a day later it is given up like the rest."""
    bridge._pending["p"] = review("new", rid="p")
    bridge.stop()
    assert bridge._deferred["p"]["_failed_at"] == T + 2


async def test_in_flight_review_is_kept_on_stop(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """Unloaded mid-request: tried again next time, looking for the bookmark first (it may have been made)."""
    bridge._replay["old"] = review("new", rid="old")
    bridge._current = ("c", review("new", rid="c"))
    bridge._pending["c"] = review("update", rid="c")  # its newer message, not handled yet
    bridge.stop()
    assert list(bridge._deferred) == ["old", "c"]
    assert bridge._deferred["c"]["type"] == "update" and bridge._deferred["c"]["_maybe_made"]


async def test_dsm_common_codes_are_transient(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait) -> None:
    """DSM's 100-119 (SS package stopped or updating...): kept for later, not refused for good."""
    client.create_bookmark.side_effect = SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", "Create", 102)
    await bridge._process("r", review("new", rid="r"))
    stats = bridge.stats()
    assert (stats["rejected"], stats["deferred"], stats["failing"]) == (0, 1, True)


async def test_refusals_that_last_become_an_issue(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, no_retry_wait) -> None:
    """SS answers but refuses every bookmark (rights taken away): not "failing", yet a Repairs issue."""
    registry = ir.async_get(hass)
    issue = f"frigate_failing_{bridge.entry_id}"
    bridge._subscribed = True
    client.create_bookmark.side_effect = SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", "Create", 400)
    await bridge._process("r", review("new", rid="r"))
    assert not bridge.stats()["failing"] and bridge._rejected_since is not None
    bridge._rejected_since -= 601
    bridge._check_health()
    assert registry.async_get_issue(DOMAIN, issue) is None  # one review refused (a camera disabled...): no issue
    client.list_bookmarks.return_value = [Bookmark(1, 6, "Person", "Frigate alert [frigate r]", T, T + 30)]
    await bridge._process("r", review("update", rid="r"))  # its bookmark found, made before: not a refusal cleared
    assert bridge._rejected_since is not None
    await bridge._process("q", review("new", rid="q"))  # a second one refused
    bridge._failing_since = bridge._rejected_since  # and a probe finds SS answering: still refused
    bridge._check_health()
    await hass.async_block_till_done()
    assert not bridge._failing and bridge._rejected_since is not None
    bridge._check_health()
    assert registry.async_get_issue(DOMAIN, issue) is not None
    client.create_bookmark.side_effect = None
    await bridge._process("s", review("new", rid="s"))  # a bookmark made
    bridge._check_health()
    assert registry.async_get_issue(DOMAIN, issue) is None
    bridge.stop()


async def test_probe_runs_again_once_the_last_finished(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    bridge._subscribed = True
    bridge._failing_since = time.monotonic()
    client.cameras.side_effect = SSConnectionError("x", "List", None)
    for _ in range(2):
        bridge._check_health()
        await hass.async_block_till_done()
    assert client.cameras.await_count == 2 and bridge._failing
    bridge.stop()


async def test_overflow_keeps_the_canary_and_the_dropped(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """Too much waiting: the review being tried to see whether SS is back stays; the one pushed out waits for SS."""
    with patch("custom_components.surveillance_station.frigate.FRIGATE_QUEUE_MAX", 2):
        bridge._keep("k", review("new", rid="k"))
        bridge._canary()
        for rid in ("q1", "q2"):
            bridge._received(MagicMock(payload=json.dumps(review("new", rid=rid))))
        assert list(bridge._pending) == ["k", "q2"] and list(bridge._deferred) == ["q1"]
        for rid in ("d1", "d2"):  # and what waits for SS is bounded too
            bridge._keep(rid, review("new", rid=rid))
    assert list(bridge._deferred) == ["d1", "d2"] and bridge.stats()["dropped"] == 1


async def test_mapped_camera(hass: HomeAssistant, setup_integration: MockConfigEntry, client: MagicMock) -> None:
    """A Frigate camera named otherwise than its SS camera: bookmarked on the one the options map it to."""
    client.cameras.return_value = [
        Camera(id=6, name="Drive Way", enabled=True), Camera(id=12, name="前门", enabled=True),
        Camera(id=13, name="后院", enabled=True), Camera(id=14, name="Garage", enabled=True),
        Camera(id=15, name="garage", enabled=True),
    ]
    mapped = FrigateBridge(
        hass, setup_integration.entry_id, client, hass.data[DATA_MANAGER], "frigate", {"person"}, "",
        cameras={"前门": "front_door, porch", "后院": "back_yard", "Garage": "garage"},
    )
    mapped.manager.thumbnail_when_recorded = AsyncMock(return_value=b"jpg")
    for rid, camera in (("p", "porch"), ("b", "back_yard"), ("d", "drive_way"), ("g", "garage")):
        await mapped.handle(review("new", camera=camera, rid=rid))
    # Mapped: that SS camera exactly (not another whose name reduces alike); the others still by name.
    assert [c.args[0] for c in client.create_bookmark.await_args_list] == [12, 13, 6, 14]
    await hass.async_block_till_done()


async def test_cut_short_during_a_retry_keeps_the_newer_message(
    hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock
) -> None:
    """The worker, unloaded while it retries a review with its newer message: that message is kept."""
    retrying = asyncio.Event()

    async def create(*args):
        if not client.create_bookmark.await_count > 1:
            raise SSConnectionError("x", "Create", None)
        retrying.set()
        await asyncio.Event().wait()

    client.create_bookmark.side_effect = create
    client.list_bookmarks.return_value = []
    with patch("custom_components.surveillance_station.frigate.FRIGATE_RETRY_SECONDS", 0.5):
        bridge._worker = hass.async_create_background_task(bridge._work(), "test worker")
        bridge._received(MagicMock(payload=json.dumps(review("new", rid="r"))))
        async with asyncio.timeout(5):
            while not bridge._counts["retried"]:  # its first try failed; waiting to retry
                await asyncio.sleep(0.01)
        bridge._received(MagicMock(payload=json.dumps(review("end", rid="r", end=T + 9))))
        async with asyncio.timeout(5):
            await retrying.wait()
    assert bridge._current[1]["type"] == "end"
    bridge.stop()
    await asyncio.sleep(0)
    kept = bridge._deferred["r"]
    assert (kept["type"], kept["after"]["end_time"], kept["_maybe_made"]) == ("end", T + 9, True)


async def test_waiting_reviews_are_written_when_ha_stops(hass: HomeAssistant, bridge: FrigateBridge, hass_storage) -> None:
    """HA stopping doesn't unload the entry: what is queued or in flight goes into the final write all the same."""
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ), patch.object(bridge, "_work", AsyncMock()):
        assert await bridge.start()
    bridge._keep("d", review("new", rid="d"))
    bridge._replay["p"] = review("new", rid="p")
    bridge._current = ("c", review("new", rid="c"))
    bridge._pending["n"] = review("new", rid="n")
    bridge._pending["c"] = review("update", rid="c")  # newer than the one in flight: it goes instead
    with patch.object(bridge._store, "async_delay_save") as save:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    data = save.call_args.args[0]()
    kept = dict(data["deferred"])
    assert list(kept) == ["d", "p", "n", "c"]
    assert kept["c"]["type"] == "update" and kept["c"]["_maybe_made"] and not kept["n"].get("_maybe_made")
    assert all(r["_failed_at"] == T + 2 for r in kept.values())
    assert list(bridge._pending) == ["n", "c"] and not bridge._pending["c"].get("_maybe_made")  # queues untouched
    bridge.stop()
    assert bridge._stop_listener is None


async def test_failure_after_a_newer_message_was_pushed_out(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """The review's newer message already waits with the deferred (a full queue): the failed older one doesn't replace it."""
    bridge._keep("r", review("end", rid="r", end=T + 9))
    bridge._defer("r", review("new", rid="r"))
    assert bridge._deferred["r"]["type"] == "end" and bridge._deferred["r"]["_maybe_made"]


async def test_stored_without_a_date_expires_a_day_after_start(hass: HomeAssistant, bridge: FrigateBridge, hass_storage) -> None:
    """Kept by a version that didn't date them: dated at load, and given up on a day later like the rest."""
    hass_storage[bridge._store.key] = {
        "version": 1, "key": bridge._store.key, "data": {"deferred": [["r", review("new", rid="r")]]}
    }
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ), patch.object(bridge, "_work", AsyncMock()):
        assert await bridge.start()
    key, loaded = next(iter(bridge._pending.items()))  # tried at once, the canary
    assert key == bridge._canary_key == "r" and loaded["_failed_at"] == T + 2
    assert loaded["_maybe_made"]  # saved while it waited: it may have been bookmarked after
    bridge._keep(*bridge._pending.popitem())
    with patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 2 + 86401):
        bridge._check_health()
    assert not bridge._deferred and bridge.stats()["dropped"] == 1
    bridge.stop()


async def test_canary_mark_cleared_once_tried(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    bridge._keep("k", review("new", rid="k"))
    bridge._canary()
    bridge._worker = hass.async_create_background_task(bridge._work(), "test worker")
    async with asyncio.timeout(5):
        while bridge._canary_key is not None:
            await asyncio.sleep(0.01)
    assert client.create_bookmark.await_count == 1 and bridge._current is None
    bridge.stop()


async def test_kept_again_keeps_what_the_earlier_told(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """A review deferred twice: the later message, still known new and maybe bookmarked, dated from the first failure."""
    bridge._keep("r", {**review("new", rid="r"), "_maybe_made": True, "_seen_new": True, "_failed_at": T - 50})
    bridge._keep("r", review("end", rid="r", end=T + 9))
    kept = bridge._deferred["r"]
    assert (kept["type"], kept["_maybe_made"], kept["_seen_new"], kept["_failed_at"]) == ("end", True, True, T - 50)


async def test_person_joining_a_quiet_review_is_announced(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """A dog again (quiet, not announced), then a person in the same review: that is news."""
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("dog",), rid="d1"))
    await bridge.handle(review("new", objects=("dog",), rid="d2", start=T + 30))
    await bridge.handle(review("update", objects=("dog",), rid="d2", start=T + 30))  # still only the dog
    await bridge.handle(review("update", objects=("dog", "person"), rid="d2", start=T + 30))
    await bridge.handle(review("update", objects=("dog", "person"), rid="d2", start=T + 30))  # once
    await hass.async_block_till_done()
    assert [(e.data["review_id"], e.data["objects"]) for e in events] == [("d1", ["Animal"]), ("d2", ["Person", "Animal"])]
    assert (bridge.stats()["held_quiet"], bridge.stats()["not_announced"]) == (1, 0)


async def test_quiet_review_ending_quiet_is_decided(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("dog",), rid="d1"))
    await bridge.handle(review("new", objects=("dog",), rid="d2", start=T + 30))
    await bridge.handle(review("end", objects=("dog",), rid="d2", start=T + 30, end=T + 60))
    await hass.async_block_till_done()
    assert [e.data["review_id"] for e in events] == ["d1"]
    assert "d2" in bridge._decided and "d2" not in bridge._not_yet
    assert (bridge.stats()["held_quiet"], bridge.stats()["not_announced"]) == (1, 1)


async def test_object_of_interest_minutes_into_a_review(hass: HomeAssistant, bridge: FrigateBridge, clock) -> None:
    """Bicycles for five minutes, then a person: news now, though the review began long ago.

    One heard of only now, long after it began (HA restarted), is not."""
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("bicycle",), rid="b"))
    clock.return_value = T + 300
    await bridge.handle(review("update", objects=("bicycle", "person"), rid="b"))
    await bridge.handle(review("update", objects=("person",), rid="late"))  # never seen before
    await hass.async_block_till_done()
    assert [e.data["review_id"] for e in events] == ["b"]


async def test_replayed_message_is_no_news(hass: HomeAssistant, bridge: FrigateBridge, clock) -> None:
    """Received minutes ago, handled only now (SS was down): bookmarked, not announced, even for a review seen going on."""
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("bicycle",), rid="b"))
    clock.return_value = T + 300
    await bridge.handle({**review("update", objects=("person",), rid="b"), "_received_at": T + 100})
    await hass.async_block_till_done()
    assert not events and bridge.stats()["bookmarked"] == 1


async def test_quiet_review_survives_a_restart(
    hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, hass_storage, clock
) -> None:
    """A review kept quiet, HA restarts: the dog stays quiet, a person joining minutes later is announced."""
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("dog",), rid="d1"))
    await bridge.handle(review("new", objects=("dog",), rid="d2", start=T + 30))
    await bridge.handle(review("new", objects=("bicycle",), rid="b"))
    await hass.async_block_till_done()
    bridge.stop()
    await bridge.async_flush()

    again = FrigateBridge(hass, bridge.entry_id, client, bridge.manager, "frigate", {"person", "dog"}, "", 5, {"Animal"})
    again.manager.thumbnail_when_recorded = AsyncMock(return_value=b"jpg")
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ), patch.object(again, "_work", AsyncMock()):
        await again.start()
    client.list_bookmarks.return_value = [Bookmark(100 + i, 6, "Animal", f"Frigate alert [frigate {r}]", T, T + 30) for i, r in enumerate(("d1", "d2"))]
    await again.handle(review("update", objects=("dog",), rid="d2", start=T + 30))  # still quiet
    clock.return_value = T + 400
    await again.handle(review("update", objects=("dog", "person"), rid="d2", start=T + 30))
    await again.handle(review("update", objects=("bicycle", "person"), rid="b"))
    await hass.async_block_till_done()
    assert [(e.data["review_id"], e.data["objects"]) for e in events] == [
        ("d1", ["Animal"]), ("d2", ["Person", "Animal"]), ("b", ["Person"])
    ]
    again.stop()


async def test_merged_message_keeps_when_the_news_came(hass: HomeAssistant, bridge: FrigateBridge, clock) -> None:
    """A person seen at +60 s waited for SS (down); an update at +1500 s joins it: still 24 minutes old news."""
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("bicycle",), rid="b"))
    clock.return_value = T + 60
    bridge._received(MagicMock(payload=json.dumps(review("update", objects=("bicycle", "person"), rid="b"))))
    bridge._keep("b", bridge._pending.pop("b"))  # SS down: deferred
    clock.return_value = T + 1500
    bridge._received(MagicMock(payload=json.dumps(review("update", objects=("bicycle", "person"), rid="b"))))
    key, message = bridge._pending.popitem()
    assert message["_received_at"] == T + 60
    await bridge.handle(message)
    await hass.async_block_till_done()
    assert not events and bridge.stats()["bookmarked"] == 1


async def test_merged_message_without_news_is_fresh(hass: HomeAssistant, bridge: FrigateBridge, clock) -> None:
    """Bicycles only in the earlier message: nothing old to carry, the new one is the news."""
    await bridge.handle(review("new", objects=("bicycle",), rid="b"))
    bridge._received(MagicMock(payload=json.dumps(review("update", objects=("bicycle",), rid="b"))))
    clock.return_value = T + 300
    bridge._received(MagicMock(payload=json.dumps(review("update", objects=("person",), rid="b"))))
    assert bridge._pending["b"]["_received_at"] == T + 300


async def test_review_ending_without_objects_is_forgotten(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    await bridge.handle(review("new", objects=("bicycle",), rid="b"))
    assert "b" in bridge._not_yet
    await bridge.handle(review("end", objects=("bicycle",), rid="b", end=T + 9))
    assert "b" not in bridge._not_yet


async def test_not_yet_is_bounded(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    with patch("custom_components.surveillance_station.frigate.FRIGATE_TRACKED_MAX", 2):
        for rid in ("a", "b", "c"):
            bridge._note_not_yet(rid)
    assert list(bridge._not_yet) == ["b", "c"]
