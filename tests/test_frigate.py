"""Frigate review items become SS bookmarks, announced once per review."""

from __future__ import annotations

import asyncio
import copy
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
    CONF_FRIGATE_URL,
    CONF_TRANSCODER,
    DETECTION_EVENT,
    DOMAIN,
)
from custom_components.surveillance_station.frigate import (
    DATA_FRIGATE,
    FrigateBridge,
    bookmark_comment,
    bookmark_name,
    camera_key,
    review_id_of,
    store_key,
)
from custom_components.surveillance_station.views import DATA_MANAGER, VodManager
from homeassistant.components import mqtt as mqtt_mod
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
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
    assert bookmark_comment("r1", "alert") == "Frigate alert [frigate r1]"


async def test_alert_lifecycle(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Created on new, renamed as objects are added, closed at the end; one event."""
    events = async_capture_events(hass, DETECTION_EVENT)
    clock = patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 2)
    with clock as now:
        await bridge.handle(review("new"))
        client.create_bookmark.assert_awaited_once_with(6, "Person", T, T + 30, "Frigate alert [frigate 1790000000.1-abc]")
        await bridge.handle(review("update", zones=("porch",)))  # only a zone: the bookmark says the same
        client.edit_bookmark.assert_not_awaited()
        now.return_value = T + 40  # still going on: the bookmark reaches now
        await bridge.handle(review("update", objects=("person", "car"), zones=("porch",)))
        await bridge.handle(review("update", objects=("person", "car"), zones=("porch",)))  # nothing new
    assert client.edit_bookmark.await_count == 1
    assert client.edit_bookmark.await_args.args == (100, 6, "Person, Car", T, T + 40, "Frigate alert [frigate 1790000000.1-abc]")
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


async def test_severity_cannot_name_another_review(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock) -> None:
    """Only Frigate's severities go into the comment, and the review is the tag that ends it: a
    message can't make a bookmark that the event list or a restart takes for another review's."""
    await bridge.handle(review("new", severity="alert [frigate other]", rid="r1"))
    assert client.create_bookmark.await_args.args[4] == "Frigate review [frigate r1]"
    assert review_id_of("Frigate x [frigate other] [frigate r1]") == "r1"
    assert review_id_of("[frigate r1] by hand") is None


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
            while bridge.stats()["failed"] < 1:
                await asyncio.sleep(0.01)
        assert client.create_bookmark.await_count == 3  # tried, then retried twice
        client.create_bookmark.side_effect = None
        client.create_bookmark.return_value = Bookmark(id=5, camera_id=6, name="Person", comment="", start=T, end=T + 30)
        async_fire_mqtt_message(hass, "nvr/reviews", json.dumps(review("new", rid="a3")))
        async with asyncio.timeout(5):
            while bridge.stats()["announced"] < 2:
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
        ({CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_URL: "frigate:5000"}, CONF_FRIGATE_URL, "invalid_url"),
        ({CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_URL: "ftp://frigate"}, CONF_FRIGATE_URL, "invalid_url"),
        ({CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_URL: "http://"}, CONF_FRIGATE_URL, "invalid_url"),
        ({CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_URL: "http://f:5000/?a=1"}, CONF_FRIGATE_URL, "invalid_url"),
        ({CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_URL: "http://[::1"}, CONF_FRIGATE_URL, "invalid_url"),
        ({CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_URL: "http://u:p@f:5000"}, CONF_FRIGATE_URL, "invalid_url"),
        ({CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_URL: "http://@f:5000"}, CONF_FRIGATE_URL, "invalid_url"),
    ):
        result = await hass.config_entries.options.async_configure(flow["flow_id"], {**base, **bad})
        assert result["errors"] == {field: error}
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {**base, CONF_FRIGATE_TOPIC: " frigate/ ", CONF_FRIGATE_LINK: " /ss-playback/playback ", CONF_FRIGATE_URL: " http://frigate:5000/ "},
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
        CONF_FRIGATE_URL: "http://frigate:5000",
        CONF_FRIGATE_QUIET: 5,
        CONF_FRIGATE_QUIET_KINDS: ["Animal"],
        CONF_FRIGATE_CAMERAS: {"Backyard": "back_yard, garden"},
        CONF_TRANSCODER: "auto",  # the GPU where there is one
    }
    start.assert_awaited_once()  # reloaded with the bridge on
    assert hass.data[DATA_FRIGATE][setup_integration.entry_id].api.url == "http://frigate:5000"
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
    await bridge.async_flush()
    await hass.async_block_till_done()
    assert not events

    # Reloaded minutes later: the review's next message announces it after all.
    bridge.manager.thumbnail_when_recorded = AsyncMock()
    again = FrigateBridge(hass, bridge.entry_id, client, bridge.manager, "frigate", {"person"}, "")
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ):
        await again.start()
    with patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 200):
        await again.handle(review("update"))
        await hass.async_block_till_done()
    assert len(events) == 1
    again.stop()


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


class Nvr:
    """A started bridge as MQTT and SS see it. In: messages on <topic>/reviews and
    <topic>/available, the minutely health check, time. Out: SS's bookmark calls,
    events, stats(), Repairs issues and what is kept for next time."""

    def __init__(self, hass: HomeAssistant, bridge: FrigateBridge, topics: dict, offset: list[float], connected: MagicMock, storage: dict) -> None:
        self.hass = hass
        self.bridge = bridge
        self.connected = connected  # mqtt.is_connected
        self._topics = topics
        self._offset = offset
        self._storage = storage

    def send(self, message: dict[str, Any]) -> None:
        self._topics["frigate/reviews"](MagicMock(payload=json.dumps(message)))

    def available(self, state: str) -> None:
        self._topics["frigate/available"](MagicMock(payload=state))

    def stats(self) -> dict[str, Any]:
        return self.bridge.stats()

    async def until(self, done: Any) -> None:
        async with asyncio.timeout(5):
            while not done():
                await asyncio.sleep(0.01)

    def later(self, seconds: float) -> None:
        """Monotonic time (how long a problem has lasted) moves on."""
        self._offset[0] += seconds

    async def check(self) -> None:
        """The health check that runs every minute."""
        async_fire_time_changed(self.hass, dt_util.utcnow() + timedelta(seconds=61))
        await self.hass.async_block_till_done()

    def issue(self, issue: str) -> ir.IssueEntry | None:
        return ir.async_get(self.hass).async_get_issue(DOMAIN, f"{issue}_{self.bridge.entry_id}")

    async def kept(self) -> dict[str, dict[str, Any]]:
        """What waits for SS, as kept for next time (written now), in order."""
        await self.bridge.async_flush()
        return dict(self._storage[store_key(self.bridge.entry_id)]["data"]["deferred"])


@pytest.fixture
async def nvr(hass: HomeAssistant, bridge: FrigateBridge, hass_storage) -> Any:
    topics: dict[str, Any] = {}

    async def subscribe(_hass, topic, received):
        topics[topic] = received
        return MagicMock()

    offset = [0.0]
    with patch(
        "custom_components.surveillance_station.frigate._monotonic", side_effect=lambda: time.monotonic() + offset[0]
    ), patch.object(mqtt_mod, "is_connected", return_value=True) as connected:
        with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
            mqtt_mod, "async_subscribe", subscribe
        ):
            assert await bridge.start()
        yield Nvr(hass, bridge, topics, offset, connected, hass_storage)
        bridge.stop()


def made_by(args: tuple) -> str:
    """The review a bookmark call (camera, name, start, end, comment) is for."""
    return args[4].split("[frigate ")[1].rstrip("]")


def made(client: MagicMock) -> list[str]:
    """The reviews SS was asked to bookmark, in order."""
    return [made_by(c.args) for c in client.create_bookmark.await_args_list]


def down(client: MagicMock) -> Any:
    """SS unreachable for bookmarks: returns how it made them before."""
    create = client.create_bookmark.side_effect
    client.create_bookmark.side_effect = SSConnectionError("x", "Create", None)
    client.list_bookmarks.return_value = []
    return create


def slow(client: MagicMock, rid: str) -> asyncio.Event:
    """SS takes its time over that review's bookmark: until the event is set."""
    gate = asyncio.Event()
    create = client.create_bookmark.side_effect

    async def wait(*args):
        if f"[frigate {rid}]" in args[4]:
            await gate.wait()
        return await create(*args)

    client.create_bookmark.side_effect = wait
    return gate


async def start(bridge: FrigateBridge) -> None:
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ):
        assert await bridge.start()


async def test_transient_error_is_retried(hass: HomeAssistant, nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
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
    nvr.send(review("new"))
    await nvr.until(lambda: events)
    assert len(calls) == 2
    stats = nvr.stats()
    assert (stats["retried"], stats["failed"], stats["failing"]) == (1, 0, False)


async def test_retry_takes_the_reviews_newer_message(hass: HomeAssistant, nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    create = client.create_bookmark.side_effect
    calls = []

    async def flaky(*args):
        calls.append(args)
        if len(calls) == 1:
            nvr.send(review("update", objects=("person", "car")))  # meanwhile the review went on
            raise SSConnectionError("x", "Create", None)
        return await create(*args)

    client.create_bookmark.side_effect = flaky
    nvr.send(review("new"))
    await nvr.until(lambda: nvr.stats()["bookmarked"])
    await hass.async_block_till_done()
    assert [c[1] for c in calls] == ["Person", "Person, Car"]  # the newer one, once
    assert nvr.stats()["queued"] == 0


async def test_no_retry_while_failing(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """SS known down: fail fast (no queue stalled behind timeouts)."""
    down(client)
    client.list_bookmarks.side_effect = SSConnectionError("x", "List", None)
    nvr.send(review("new", rid="a"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    # Tried, then twice more (each looking for a bookmark the failed try may have made).
    assert (client.create_bookmark.await_count, client.list_bookmarks.await_count) == (1, 2)
    nvr.send(review("new", rid="b"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 2)
    assert client.create_bookmark.await_count == 2 and nvr.stats()["failing"]
    assert list(await nvr.kept()) == ["a", "b"]


async def test_refused_credentials_are_kept_for_later(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """Credentials refused (a reauth fixes that): tried again, and kept for later like SS
    unreachable, not refused for good."""
    client.list_bookmarks.return_value = []
    client.create_bookmark.side_effect = SSAuthError("SYNO.API.Auth", "login", 400)
    nvr.send(review("new", rid="c"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    assert client.create_bookmark.await_count == 3
    stats = nvr.stats()
    assert (stats["retried"], stats["rejected"], stats["failing"]) == (2, 0, True)


async def test_camera_list_reread_after_a_failure(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """A camera replaced in SS (same name, new id): the next try looks it up again."""
    create = client.create_bookmark.side_effect
    client.create_bookmark.side_effect = SSError("x", "Create", 400)
    nvr.send(review("new", rid="a"))
    await nvr.until(lambda: nvr.stats()["rejected"] == 1)
    client.cameras.return_value = [Camera(id=16, name="Drive Way", enabled=True)]
    client.create_bookmark.side_effect = create
    nvr.send(review("new", rid="b"))
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 1)
    assert client.create_bookmark.await_args.args[0] == 16


async def test_nothing_after_stop(nvr: Nvr) -> None:
    """A subscription HA's MQTT carried over to a new client after unsubscribing: ignored."""
    nvr.bridge.stop()
    nvr.send(review("new"))
    assert (nvr.stats()["messages"], nvr.stats()["queued"]) == (0, 0)


async def test_a_full_queue_pushes_the_oldest_out(nvr: Nvr, client: MagicMock) -> None:
    """SS slow while reviews pour in: a review's messages are one, and the oldest waiting
    beyond the bound waits with those SS failed (bookmarked later, not lost), fresh ones first."""
    gate = slow(client, "q0")
    with patch("custom_components.surveillance_station.frigate.FRIGATE_QUEUE_MAX", 2):
        nvr.send(review("new", rid="q0"))
        await nvr.until(lambda: client.create_bookmark.await_count == 1)
        for rid in ("q1", "q2", "q1", "q3"):
            nvr.send(review("update", rid=rid))
        stats = nvr.stats()
        # q1's second message replaced its first; q3 pushed the oldest (q1) out.
        assert (stats["messages"], stats["coalesced"], stats["dropped"], stats["queued"], stats["deferred"]) == (5, 1, 0, 2, 1)
        gate.set()
        await nvr.until(lambda: nvr.stats()["bookmarked"] == 4)
    assert made(client) == ["q0", "q2", "q3", "q1"]
    assert (nvr.stats()["queued"], nvr.stats()["deferred"]) == (0, 0)


async def test_full_queue_keeps_the_canary_and_bounds_the_rest(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """Too much waiting: the review being tried to see whether SS is back stays first in line;
    the one pushed out waits for SS; and what waits for SS is bounded too (the oldest dropped)."""
    create = client.create_bookmark.side_effect
    gate = asyncio.Event()

    async def ss(*args):
        if "[frigate k]" in args[4] and not gate.is_set():
            raise SSConnectionError("x", "Create", None)
        if "[frigate b]" in args[4]:
            await gate.wait()
        return await create(*args)

    client.create_bookmark.side_effect = ss
    client.list_bookmarks.return_value = []
    with patch("custom_components.surveillance_station.frigate.FRIGATE_QUEUE_MAX", 2):
        nvr.send(review("new", rid="k"))
        await nvr.until(lambda: nvr.stats()["deferred"] == 1)
        nvr.send(review("new", rid="b"))
        await nvr.until(lambda: client.create_bookmark.await_count == 4)
        await nvr.check()  # "k" tried again, first in line (the worker still busy with "b")
        for rid in ("q1", "q2"):
            nvr.send(review("new", rid=rid))
        assert (nvr.stats()["queued"], nvr.stats()["deferred"], nvr.stats()["dropped"]) == (2, 1, 0)
        gate.set()
        await nvr.until(lambda: nvr.stats()["bookmarked"] == 4)
        assert made(client)[3:] == ["b", "k", "q2", "q1"]

        client.create_bookmark.side_effect = SSConnectionError("x", "Create", None)
        for rid in ("d1", "d2", "d3"):
            nvr.send(review("new", rid=rid))
        await nvr.until(lambda: nvr.stats()["dropped"] == 1)
    assert list(await nvr.kept()) == ["d2", "d3"]


async def test_canary_mark_cleared_once_tried(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """The review tried first to see whether SS is back, once through, has no place of its own:
    when the queue is full again, its next message is pushed out like any oldest one."""
    create = down(client)
    nvr.send(review("new", rid="k"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    client.create_bookmark.side_effect = create
    await nvr.check()
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 1)
    order = []
    gate = slow(client, "g")
    create = client.create_bookmark.side_effect

    async def logged(*args):
        order.append(made_by(args))
        return await create(*args)

    async def edited(bookmark_id, camera_id, name, start, end, comment=""):
        order.append(f"edit {made_by((0, 0, 0, 0, comment))}")
        return Bookmark(bookmark_id, camera_id, name, comment, int(start), int(end))

    client.create_bookmark.side_effect = logged
    client.edit_bookmark.side_effect = edited
    with patch("custom_components.surveillance_station.frigate.FRIGATE_QUEUE_MAX", 2):
        nvr.send(review("new", rid="g"))
        await nvr.until(lambda: order == ["g"])
        nvr.send(review("update", rid="k", objects=("person", "car")))
        nvr.send(review("new", rid="a"))
        nvr.send(review("new", rid="b"))  # full: "k", the oldest, waits with the deferred
        gate.set()
        await nvr.until(lambda: len(order) == 4)
    assert order == ["g", "a", "b", "edit k"]


async def test_seen_new_survives_coalescing(nvr: Nvr, client: MagicMock) -> None:
    """A review's "new" replaced by its "update" while waiting: still known new (no bookmark search)."""
    gate = slow(client, "q0")
    nvr.send(review("new", rid="q0"))
    await nvr.until(lambda: client.create_bookmark.await_count == 1)
    nvr.send(review("new"))
    nvr.send(review("update", objects=("person", "car")))
    gate.set()
    await nvr.until(lambda: client.create_bookmark.await_count == 2)
    client.list_bookmarks.assert_not_called()
    assert client.create_bookmark.await_args.args[1] == "Person, Car"


async def test_unexpected_error_is_not_retried_but_survived(nvr: Nvr, client: MagicMock) -> None:
    client.create_bookmark.side_effect = RuntimeError("bug")
    nvr.send(review("new", rid="a"))
    await nvr.until(lambda: nvr.stats()["failed"] == 1)
    assert client.create_bookmark.await_count == 1
    stats = nvr.stats()
    assert stats["last_error"]["error"] == "RuntimeError: bug"
    assert not stats["failing"] and not stats["deferred"]  # a bug in one message, not SS down


async def test_retry_finds_a_bookmark_made_before_the_error(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """SS made it, only its answer was lost: the retry finds it rather than make a second one."""
    made_in_ss = []

    async def made_then_lost(camera_id, name, start, end, comment=""):
        made_in_ss.append(Bookmark(id=50, camera_id=camera_id, name=name, comment=comment, start=int(start), end=int(end)))
        raise SSConnectionError("x", "Create", None)

    client.create_bookmark.side_effect = made_then_lost
    client.list_bookmarks.side_effect = lambda ids: list(made_in_ss)
    nvr.send(review("new"))
    await nvr.until(lambda: nvr.stats()["announced"])
    nvr.send(review("end", end=T + 9))
    await nvr.until(lambda: client.edit_bookmark.await_count == 1)
    assert len(made_in_ss) == 1 and client.edit_bookmark.await_args.args[0] == 50


async def test_deferred_reviews_go_through_once_ss_answers(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """Failed while SS was down: the oldest is tried again every minute; once it goes through,
    all the others follow, after anything fresh."""
    create = down(client)
    for rid in ("a", "b", "c"):
        nvr.send(review("new", rid=rid))
    await nvr.until(lambda: nvr.stats()["deferred"] == 3)
    # A message that doesn't reach SS says nothing about bookmarks.
    nvr.send(review("genai", rid="x"))
    await nvr.until(lambda: nvr.stats()["ignored"].get("other_type"))
    assert nvr.stats()["failing"]

    await nvr.check()  # still down: "a" tried, and waits again (now last)
    await nvr.until(lambda: client.create_bookmark.await_count == 6 and nvr.stats()["deferred"] == 3)
    assert list(await nvr.kept()) == ["b", "c", "a"]

    async def back(*args):
        if "[frigate b]" in args[4]:
            nvr.send(review("new", rid="f"))  # fresh, while "b" goes through
        return await create(*args)

    client.create_bookmark.side_effect = back
    await nvr.check()
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 4)
    assert made(client)[6:] == ["b", "f", "c", "a"]
    stats = nvr.stats()
    assert not stats["failing"] and (stats["deferred"], stats["queued"]) == (0, 0)


async def test_given_up_after_a_day_then_probed(nvr: Nvr, client: MagicMock, clock, no_retry_wait) -> None:
    """A review SS didn't take for a day is given up on; failing with nothing left to try, SS is
    asked whether it answers at all, and that clears "failing"."""
    down(client)
    nvr.send(review("new", rid="r"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    clock.return_value = T + 2 + 86401
    tried = client.create_bookmark.await_count
    await nvr.check()
    assert (nvr.stats()["deferred"], nvr.stats()["dropped"]) == (0, 1)
    await nvr.until(lambda: not nvr.stats()["failing"])
    assert client.create_bookmark.await_count == tried


async def test_failing_probe_keeps_the_error_and_runs_again(nvr: Nvr, client: MagicMock, clock, no_retry_wait) -> None:
    down(client)
    nvr.send(review("new", rid="r"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    clock.return_value = T + 2 + 86401
    client.cameras.side_effect = SSConnectionError("SYNO.SurveillanceStation.Camera", "List", None, "TimeoutError")
    asked = client.cameras.await_count
    for n in (1, 2):  # once the last one has finished
        await nvr.check()
        await nvr.until(lambda: client.cameras.await_count == asked + n)
        await nvr.hass.async_block_till_done()
    nvr.later(601)
    await nvr.check()
    assert nvr.stats()["failing"] and "TimeoutError" in nvr.issue("frigate_failing").translation_placeholders["error"]


async def test_review_going_on_after_a_lost_answer_is_looked_for(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """SS made the bookmark but its answer was lost, then SS went down: the review's next
    message, once SS is back, finds that bookmark rather than making a second."""
    made_in_ss = []

    async def made_then_lost(camera_id, name, start, end, comment=""):
        made_in_ss.append(Bookmark(id=50, camera_id=camera_id, name=name, comment=comment, start=int(start), end=int(end)))
        raise SSConnectionError("x", "Create", None)

    client.create_bookmark.side_effect = made_then_lost
    client.list_bookmarks.side_effect = SSConnectionError("x", "List", None)
    nvr.send(review("new", rid="r"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    client.list_bookmarks.side_effect = lambda ids: list(made_in_ss)
    nvr.send(review("update", rid="r", objects=("person", "car")))
    await nvr.until(lambda: client.edit_bookmark.await_count == 1)
    assert len(made_in_ss) == 1 and client.edit_bookmark.await_args.args[:3] == (50, 6, "Person, Car")
    assert nvr.stats()["deferred"] == 0


async def test_replay_stops_when_ss_fails_again(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """SS down again mid-replay: the rest wait again, without a request each."""
    create = down(client)
    client.list_bookmarks.side_effect = SSConnectionError("x", "List", None)
    for rid in ("a", "b", "c"):
        nvr.send(review("new", rid=rid))
    await nvr.until(lambda: nvr.stats()["deferred"] == 3)
    assert client.create_bookmark.await_count == 3  # "a" retried twice, each failing to look for it
    tried = 0

    async def once(*args):  # "a" goes through, then SS is down again
        nonlocal tried
        tried += 1
        if tried > 1:
            raise SSConnectionError("x", "Create", None)
        return await create(*args)

    client.create_bookmark.side_effect = once
    client.list_bookmarks.side_effect = None
    await nvr.check()
    await nvr.until(lambda: nvr.stats()["failed"] == 4)
    assert made(client)[3:] == ["a", "b", "b", "b"]  # "b" tried (and retried), "c" not: back to waiting
    assert set(await nvr.kept()) == {"b", "c"} and nvr.stats()["queued"] == 0


async def test_ss_saying_no_is_not_retried_forever(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """An error code for this request (a camera disabled in SS...): logged, not kept for later."""
    client.create_bookmark.side_effect = SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", "Create", 400)
    nvr.send(review("new", rid="r"))
    await nvr.until(lambda: nvr.stats()["rejected"] == 1)
    assert client.create_bookmark.await_count == 1 and nvr.stats()["deferred"] == 0


async def test_dsm_common_codes_are_transient(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """DSM's 100-119 (SS package stopped or updating...): kept for later, not refused for good."""
    client.create_bookmark.side_effect = SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", "Create", 102)
    nvr.send(review("new", rid="r"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    stats = nvr.stats()
    assert (stats["rejected"], stats["failing"]) == (0, True)


async def test_waiting_reviews_survive_a_restart(
    hass: HomeAssistant, nvr: Nvr, bridge: FrigateBridge, client: MagicMock, no_retry_wait
) -> None:
    """SS down, then HA restarts: what waited, what was in flight and what wasn't handled yet are
    all tried again at start (bookmarked, not stale-announced)."""
    events = async_capture_events(hass, DETECTION_EVENT)
    create = down(client)
    nvr.send(review("new", rid="r", start=T - 900))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    client.create_bookmark.side_effect = create
    slow(client, "q")
    nvr.send(review("new", rid="q"))
    await nvr.until(lambda: client.create_bookmark.await_count == 4)
    nvr.send(review("new", rid="p"))  # not handled yet when stopped
    bridge.stop()
    await bridge.async_flush()

    again = FrigateBridge(hass, bridge.entry_id, client, bridge.manager, "frigate", {"person"}, "")
    client.create_bookmark.reset_mock()
    client.create_bookmark.side_effect = create
    await start(again)
    await nvr.until(lambda: client.create_bookmark.await_count == 3)
    await hass.async_block_till_done()
    assert made(client) == ["r", "q", "p"]
    assert "r" not in [e.data["review_id"] for e in events]
    again.stop()


async def test_older_message_failing_defers_nothing_when_a_newer_waits(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """A review's "new" fails while its "update" waits: the update goes instead, knowing a bookmark may exist.

    Deferring the "new" as well would replay it after the update was
    bookmarked, and make a second bookmark."""
    made_in_ss = []

    async def create(camera_id, name, start, end, comment=""):
        if "[frigate x]" in comment:
            raise SSConnectionError("x", "Create", None)
        made_in_ss.append(Bookmark(50, camera_id, name, comment, int(start), int(end)))
        # Made, its answer lost; meanwhile the review went on.
        nvr.send(review("update", rid="r", objects=("person", "car")))
        raise SSConnectionError("x", "Create", None)

    client.create_bookmark.side_effect = create
    client.list_bookmarks.side_effect = lambda ids: list(made_in_ss)
    nvr.send(review("new", rid="x"))  # SS failing: no retries
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    nvr.send(review("new", rid="r"))
    await nvr.until(lambda: client.edit_bookmark.await_count == 1)
    assert len(made_in_ss) == 1 and client.edit_bookmark.await_args.args[:3] == (50, 6, "Person, Car")
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    assert list(await nvr.kept()) == ["x"]


async def test_failure_after_a_newer_message_was_pushed_out(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """The review's newer message already waits with the deferred (a full queue): the failed older
    one doesn't replace it, and its end reaches the bookmark once SS is back."""
    create = client.create_bookmark.side_effect
    gate = asyncio.Event()
    made_in_ss = []

    async def ss(camera_id, name, start, end, comment=""):
        if "[frigate x]" in comment and not gate.is_set():
            raise SSConnectionError("x", "Create", None)
        if "[frigate r]" in comment and not made_in_ss:
            await gate.wait()
            made_in_ss.append(Bookmark(50, camera_id, name, comment, int(start), int(end)))
            raise SSConnectionError("x", "Create", None)  # made, its answer lost
        return await create(camera_id, name, start, end, comment)

    client.create_bookmark.side_effect = ss
    client.list_bookmarks.side_effect = lambda ids: list(made_in_ss)
    with patch("custom_components.surveillance_station.frigate.FRIGATE_QUEUE_MAX", 2):
        nvr.send(review("new", rid="x"))
        await nvr.until(lambda: nvr.stats()["deferred"] == 1)
        nvr.send(review("new", rid="r"))
        await nvr.until(lambda: client.create_bookmark.await_count == 4)
        nvr.send(review("end", rid="r", end=T + 9))
        nvr.send(review("new", rid="y"))
        nvr.send(review("new", rid="z"))  # full: r's end waits with the deferred
        assert nvr.stats()["deferred"] == 2
        gate.set()
        await nvr.until(lambda: client.edit_bookmark.await_count == 1)
    assert len(made_in_ss) == 1
    assert client.edit_bookmark.await_args.args[0] == 50 and client.edit_bookmark.await_args.args[4] == T + 10


async def test_stopped_before_ss_answered_still_expires(nvr: Nvr, bridge: FrigateBridge, client: MagicMock) -> None:
    """Kept on stop without ever reaching SS: dated, so a day later it is given up like the rest."""
    slow(client, "q")
    nvr.send(review("new", rid="q"))
    await nvr.until(lambda: client.create_bookmark.await_count == 1)
    nvr.send(review("new", rid="p"))
    bridge.stop()
    assert (await nvr.kept())["p"]["_failed_at"] == T + 2


async def test_in_flight_review_is_kept_on_stop(nvr: Nvr, bridge: FrigateBridge, client: MagicMock) -> None:
    """Unloaded mid-request: tried again next time, looking for the bookmark first (it may have been made)."""
    slow(client, "c")
    nvr.send(review("new", rid="c"))
    await nvr.until(lambda: client.create_bookmark.await_count == 1)
    nvr.send(review("update", rid="c"))  # its newer message, not handled yet
    nvr.send(review("new", rid="n"))
    bridge.stop()
    kept = await nvr.kept()
    assert list(kept) == ["c", "n"]
    assert kept["c"]["type"] == "update" and kept["c"]["_maybe_made"] and not kept["n"].get("_maybe_made")


async def test_in_flight_alone_is_kept_on_stop(nvr: Nvr, bridge: FrigateBridge, client: MagicMock) -> None:
    slow(client, "c")
    nvr.send(review("new", rid="c"))
    await nvr.until(lambda: client.create_bookmark.await_count == 1)
    bridge.stop()
    kept = await nvr.kept()
    assert kept["c"]["type"] == "new" and kept["c"]["_maybe_made"]


async def test_cut_short_during_a_retry_keeps_the_newer_message(nvr: Nvr, bridge: FrigateBridge, client: MagicMock) -> None:
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
        nvr.send(review("new", rid="r"))
        await nvr.until(lambda: nvr.stats()["retried"])  # its first try failed; waiting to retry
        nvr.send(review("end", rid="r", end=T + 9))
        async with asyncio.timeout(5):
            await retrying.wait()
    bridge.stop()
    await asyncio.sleep(0)
    kept = (await nvr.kept())["r"]
    assert (kept["type"], kept["after"]["end_time"], kept["_maybe_made"]) == ("end", T + 9, True)


async def test_waiting_reviews_are_written_when_ha_stops(
    hass: HomeAssistant, nvr: Nvr, bridge: FrigateBridge, client: MagicMock, hass_storage, no_retry_wait
) -> None:
    """HA stopping doesn't unload the entry: what is queued or in flight goes into the final write
    all the same, and until HA is gone the queue goes on as before."""
    create = down(client)
    nvr.send(review("new", rid="d"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    client.create_bookmark.side_effect = create
    gate = slow(client, "c")
    nvr.send(review("new", rid="c"))
    await nvr.until(lambda: client.create_bookmark.await_count == 4)
    nvr.send(review("new", rid="n"))
    nvr.send(review("update", rid="c"))  # newer than the one in flight: it goes instead
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    kept = dict(hass_storage[store_key(bridge.entry_id)]["data"]["deferred"])
    assert list(kept) == ["d", "n", "c"]
    assert kept["c"]["type"] == "update" and kept["c"]["_maybe_made"] and not kept["n"].get("_maybe_made")
    assert all(r["_failed_at"] == T + 2 for r in kept.values())
    gate.set()
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 3)
    assert made(client)[-3:] == ["c", "n", "d"]


async def test_stored_without_a_date(hass: HomeAssistant, bridge: FrigateBridge, client: MagicMock, hass_storage, clock, no_retry_wait) -> None:
    """Kept by a version that didn't date them: tried at once, looked for first (saved while it
    waited, it may have been bookmarked after), dated at load and given up on a day later."""
    key = store_key(bridge.entry_id)
    hass_storage[key] = {
        "version": 1, "key": key, "data": {"deferred": [["r", review("new", rid="r")], ["s", review("new", rid="s")]]}
    }
    client.list_bookmarks.return_value = [Bookmark(7, 6, "Person", "Frigate alert [frigate r]", T, T + 30)]
    client.create_bookmark.side_effect = SSConnectionError("x", "Create", None)
    await start(bridge)
    async with asyncio.timeout(5):
        while bridge.stats()["failed"] < 1:
            await asyncio.sleep(0.01)
    assert made(client) == ["s"] * 3  # "r" found, not made again
    clock.return_value = T + 2 + 86401
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert (bridge.stats()["deferred"], bridge.stats()["dropped"]) == (0, 1)
    bridge.stop()


async def test_kept_again_keeps_its_first_failure(nvr: Nvr, client: MagicMock, clock, no_retry_wait) -> None:
    """A review deferred twice: kept knowing it may be bookmarked, and dated from its first
    failure, so given up on a day after that, not after the last."""
    down(client)
    nvr.send(review("new", rid="r"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    clock.return_value = T + 50000
    await nvr.check()
    await nvr.until(lambda: client.create_bookmark.await_count == 4)
    kept = (await nvr.kept())["r"]
    assert (kept["_maybe_made"], kept["_failed_at"]) == (True, T + 2)
    clock.return_value = T + 2 + 86401
    await nvr.check()
    assert (nvr.stats()["deferred"], nvr.stats()["dropped"]) == (0, 1)


async def test_merged_message_keeps_when_the_news_came(hass: HomeAssistant, nvr: Nvr, client: MagicMock, clock, no_retry_wait) -> None:
    """A person seen at +60 s waited for SS (down); an update at +1500 s joins it: still 24
    minutes old news, and the bookmark open up to when it came."""
    events = async_capture_events(hass, DETECTION_EVENT)
    nvr.send(review("new", objects=("bicycle",), rid="b"))
    await nvr.until(lambda: nvr.stats()["ignored"].get("no_objects"))
    create = down(client)
    clock.return_value = T + 60
    nvr.send(review("update", objects=("bicycle", "person"), rid="b"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    client.create_bookmark.side_effect = create
    clock.return_value = T + 1500
    nvr.send(review("update", objects=("bicycle", "person"), rid="b"))
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 1)
    await hass.async_block_till_done()
    assert not events
    assert client.create_bookmark.await_args.args[2:4] == (T, T + 60)


async def test_merged_message_without_news_is_fresh(hass: HomeAssistant, nvr: Nvr, client: MagicMock, clock) -> None:
    """Bicycles only in the earlier message: nothing old to carry, the new one is the news."""
    events = async_capture_events(hass, DETECTION_EVENT)
    nvr.send(review("new", objects=("bicycle",), rid="b"))
    await nvr.until(lambda: nvr.stats()["ignored"].get("no_objects"))
    gate = slow(client, "g")
    nvr.send(review("new", rid="g"))
    await nvr.until(lambda: client.create_bookmark.await_count == 1)
    nvr.send(review("update", objects=("bicycle",), rid="b"))
    clock.return_value = T + 300
    nvr.send(review("update", objects=("person",), rid="b"))
    gate.set()
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 2)
    await hass.async_block_till_done()
    assert "b" in [e.data["review_id"] for e in events]


async def test_replayed_open_review_ends_when_its_message_came(nvr: Nvr, client: MagicMock, clock, no_retry_wait) -> None:
    """A message of a review still going on, replayed an hour after it came (SS was down), its end
    never heard of: the bookmark reaches when the message came, not the replay."""
    create = down(client)
    nvr.send(review("update", rid="r"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    clock.return_value = T + 3600
    client.create_bookmark.side_effect = create
    await nvr.check()
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 1)
    assert client.create_bookmark.await_args.args[2:4] == (T, T + 30)


async def test_replayed_message_is_no_news(hass: HomeAssistant, nvr: Nvr, client: MagicMock, clock, no_retry_wait) -> None:
    """Received minutes ago, handled only now (SS was down): bookmarked, not announced, even for a review seen going on."""
    events = async_capture_events(hass, DETECTION_EVENT)
    nvr.send(review("new", objects=("bicycle",), rid="b"))
    await nvr.until(lambda: nvr.stats()["ignored"].get("no_objects"))
    create = down(client)
    nvr.send(review("update", objects=("person",), rid="b"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    clock.return_value = T + 300
    client.create_bookmark.side_effect = create
    await nvr.check()
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 1)
    await hass.async_block_till_done()
    assert not events


async def test_replayed_joining_is_not_sent_again(hass: HomeAssistant, nvr: Nvr, client: MagicMock, clock, no_retry_wait) -> None:
    """A person in a message that waited out an SS outage: old news, not a late second notification."""
    events = async_capture_events(hass, DETECTION_EVENT)
    nvr.send(review("new", objects=("dog",), rid="d1"))
    await nvr.until(lambda: events)
    edit = client.edit_bookmark.side_effect
    client.edit_bookmark.side_effect = SSConnectionError("x", "Edit", None)
    client.list_bookmarks.side_effect = SSConnectionError("x", "List", None)
    nvr.send(review("update", objects=("dog", "person"), rid="d1"))
    await nvr.until(lambda: nvr.stats()["deferred"] == 1)
    clock.return_value = T + 600
    client.edit_bookmark.side_effect = edit
    client.list_bookmarks.side_effect = None
    client.list_bookmarks.return_value = [Bookmark(100, 6, "Animal", "Frigate alert [frigate d1]", T, T + 30)]
    await nvr.check()
    await nvr.until(lambda: nvr.stats()["deferred"] == 0 and client.edit_bookmark.await_count == 2)
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_refusals_that_last_become_an_issue(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """SS answers but refuses every bookmark (rights taken away): not "failing", yet a Repairs issue."""
    create = client.create_bookmark.side_effect
    client.create_bookmark.side_effect = SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", "Create", 400)
    nvr.send(review("new", rid="r"))
    await nvr.until(lambda: nvr.stats()["rejected"] == 1)
    assert not nvr.stats()["failing"]
    nvr.later(601)
    await nvr.check()
    assert nvr.issue("frigate_failing") is None  # one review refused (a camera disabled...): no issue
    client.list_bookmarks.return_value = [Bookmark(1, 6, "Person", "Frigate alert [frigate r]", T, T + 30)]
    listed = client.list_bookmarks.await_count
    nvr.send(review("update", rid="r"))  # its bookmark found, made before: not a refusal cleared
    await nvr.until(lambda: client.list_bookmarks.await_count > listed)
    nvr.send(review("new", rid="q"))  # a second one refused
    await nvr.until(lambda: nvr.stats()["rejected"] == 2)
    await nvr.check()
    assert nvr.issue("frigate_failing") is not None and not nvr.stats()["failing"]
    client.create_bookmark.side_effect = create
    nvr.send(review("new", rid="s"))  # a bookmark made
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 1)
    await nvr.check()
    assert nvr.issue("frigate_failing") is None


async def test_lasting_problems_become_repairs_issues(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    create = down(client)
    nvr.send(review("new", rid="a"))
    await nvr.until(lambda: nvr.stats()["failing"])
    await nvr.check()  # "a" tried again: still down
    await nvr.until(lambda: client.create_bookmark.await_count == 4)
    assert nvr.issue("frigate_failing") is None  # not for long yet
    nvr.later(601)
    await nvr.check()
    assert nvr.issue("frigate_failing") is not None
    client.create_bookmark.side_effect = create
    await nvr.check()  # "a" tried again: through
    await nvr.until(lambda: not nvr.stats()["failing"])
    await nvr.check()
    assert nvr.issue("frigate_failing") is None

    nvr.available("offline")
    nvr.available("online")
    nvr.later(601)
    await nvr.check()
    assert nvr.issue("frigate_offline") is None and nvr.stats()["frigate_available"] == "online"
    nvr.available("offline")
    nvr.later(601)
    await nvr.check()
    assert nvr.issue("frigate_offline") is not None and nvr.stats()["frigate_available"] == "offline"
    nvr.bridge.stop()  # unloaded: its issues go with it
    assert nvr.issue("frigate_offline") is None


async def test_mqtt_never_there_is_a_repairs_issue(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    registry = ir.async_get(hass)
    issue = f"frigate_mqtt_{bridge.entry_id}"
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=False)), patch(
        "custom_components.surveillance_station.frigate.FRIGATE_MQTT_RETRY_SECONDS", 0.01
    ), patch("custom_components.surveillance_station.frigate._monotonic", return_value=time.monotonic() + 601):
        started = hass.async_create_background_task(bridge.start(), "test start")
        # Not within asyncio.timeout: firing the time fires its timer too.
        for _ in range(100):
            await asyncio.sleep(0)
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
            if registry.async_get_issue(DOMAIN, issue) is not None:
                break
        assert registry.async_get_issue(DOMAIN, issue) is not None
        bridge.stop()
        assert await started is False
    assert registry.async_get_issue(DOMAIN, issue) is None


async def test_mqtt_lost_later_is_a_repairs_issue(nvr: Nvr) -> None:
    """Subscribed, then HA's MQTT loses its broker (or is unloaded): an issue once it lasts, gone when it's back."""
    nvr.connected.return_value = False
    await nvr.check()
    assert nvr.issue("frigate_mqtt") is None  # not for long yet
    nvr.later(601)
    await nvr.check()
    assert nvr.issue("frigate_mqtt") is not None
    nvr.connected.return_value = True
    await nvr.check()
    assert nvr.issue("frigate_mqtt") is None
    nvr.connected.side_effect = KeyError("mqtt")  # HA's MQTT integration not loaded
    await nvr.check()
    nvr.later(601)
    await nvr.check()
    assert nvr.issue("frigate_mqtt") is not None


async def test_unloaded_while_reading_its_state(hass: HomeAssistant, bridge: FrigateBridge, hass_storage) -> None:
    """Stopped while its state is read (a reload right after setup): it doesn't start, and the
    flush on unload doesn't replace the stored state with nothing."""
    key = store_key(bridge.entry_id)
    stored = {"decided": [["d1", "person"]], "last_seen": [], "not_yet": [], "deferred": [["r", review("new", rid="r")]]}
    hass_storage[key] = {"version": 1, "key": key, "data": copy.deepcopy(stored)}
    load = Store.async_load

    async def unloaded_meanwhile(store):
        if store.key == key:
            bridge.stop()
            await bridge.async_flush()
        return await load(store)

    with patch.object(Store, "async_load", unloaded_meanwhile), patch(
        "custom_components.surveillance_station.frigate._monotonic", return_value=time.monotonic() + 601
    ):
        assert await bridge.start() is False
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    assert hass_storage[key]["data"] == stored
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"frigate_mqtt_{bridge.entry_id}") is None  # no health check left running


async def test_bookmark_deleted_in_ss_is_made_again(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """Its bookmark deleted in SS while the review goes on: refused once, then made again, not
    edited (and refused) at every later message."""
    nvr.send(review("new"))
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 1)
    edit = client.edit_bookmark.side_effect
    client.edit_bookmark.side_effect = SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", "Edit", 400)
    client.list_bookmarks.return_value = []
    nvr.send(review("update", objects=("person", "car")))
    await nvr.until(lambda: nvr.stats()["rejected"] == 1)
    client.edit_bookmark.side_effect = edit
    nvr.send(review("update", objects=("person", "car", "dog")))
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 2)
    assert client.edit_bookmark.await_count == 1
    assert client.create_bookmark.await_args.args[1] == "Person, Car, Animal"


async def test_bookmark_edit_answered_oddly_is_looked_for_again(nvr: Nvr, client: MagicMock, no_retry_wait) -> None:
    """SS answering an Edit without the bookmark (the library's SSError without a code, taken as
    "not now"): the retry looks for the bookmark rather than edit the same id again, so one
    deleted in SS is made again, and SS isn't taken for failing."""
    nvr.send(review("new"))
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 1)
    client.edit_bookmark.side_effect = SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", "Edit", None, "{}")
    client.list_bookmarks.return_value = []
    nvr.send(review("update", objects=("person", "car")))
    await nvr.until(lambda: nvr.stats()["bookmarked"] == 2)
    stats = nvr.stats()
    assert (stats["retried"], stats["failing"], stats["deferred"], client.edit_bookmark.await_count) == (1, False, 0, 1)


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
    # What each was sent with is remembered too: a person joining the dog's is sent again.
    client.list_bookmarks.return_value = [
        Bookmark(id=101, camera_id=6, name="Animal", comment="Frigate alert [frigate d1]", start=T, end=T + 30)
    ]
    await again.handle(review("update", objects=("dog", "person"), rid="d1"))
    await hass.async_block_till_done()
    assert [(e.data["review_id"], e.data["objects"]) for e in events[2:]] == [("d1", ["Person", "Animal"])]
    again.stop()


async def test_decided_before_0_18_is_not_sent_again(hass: HomeAssistant, bridge: FrigateBridge, hass_storage) -> None:
    """Stored as bare ids (what they were sent with unknown): decided, and never sent again."""
    hass_storage[f"{DOMAIN}.frigate.{bridge.entry_id}"] = {"version": 1, "key": "k", "data": {"decided": ["d1"]}}
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ):
        await bridge.start()
    assert bridge._decided == {"d1": None}
    bridge.stop()


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


async def test_unreadable_state_is_ignored(hass: HomeAssistant, bridge: FrigateBridge, hass_storage, caplog) -> None:
    hass_storage[bridge._store.key] = {"version": 1, "key": bridge._store.key, "data": {"last_seen": [["x", "Animal", T]]}}
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ):
        assert await bridge.start()
    assert "unreadable Frigate state" in caplog.text
    bridge.stop()


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
    assert hass_storage[key]["data"]["decided"] == [["r1", None]]  # written on unload, not 2 s later
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()
    assert key not in hass_storage  # and not written back by a pending save


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


async def test_more_important_joining_is_sent_again(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """A dog, then a person in the same review: sent again (the same review, so it replaces the
    first notification); a car or another animal after that is not, nor a person twice."""
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("dog",), rid="d1"))
    await hass.async_block_till_done()
    await bridge.handle(review("update", objects=("dog", "cat"), rid="d1"))  # another animal: no
    await bridge.handle(review("update", objects=("dog", "person"), rid="d1"))
    await hass.async_block_till_done()
    await bridge.handle(review("update", objects=("dog", "person", "car"), rid="d1"))  # less than a person: no
    await bridge.handle(review("end", objects=("dog", "person", "car"), rid="d1", end=T + 9))
    await hass.async_block_till_done()
    assert [(e.data["review_id"], e.data["objects"]) for e in events] == [("d1", ["Animal"]), ("d1", ["Person", "Animal"])]
    assert bridge._decided["d1"] == "person"
    stats = bridge.stats()
    assert (stats["announced"], stats["announced_again"], stats["not_announced"]) == (1, 1, 0)


async def test_less_important_joining_is_not_sent_again(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """A person, then a car and a dog: the first notification says enough. A car, then a person: again."""
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("person",), rid="p1"))
    await hass.async_block_till_done()
    await bridge.handle(review("update", objects=("person", "car", "dog"), rid="p1"))
    await bridge.handle(review("new", objects=("car",), rid="c1"))
    await hass.async_block_till_done()
    await bridge.handle(review("update", objects=("car", "person"), rid="c1"))
    await hass.async_block_till_done()
    assert [(e.data["review_id"], e.data["objects"]) for e in events] == [
        ("p1", ["Person"]), ("c1", ["Car"]), ("c1", ["Person", "Car"])
    ]


async def test_joining_while_being_sent_is_in_the_one_notification(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """A person seen while the dog's notification waits for its frame: in that one, not a second."""
    events = async_capture_events(hass, DETECTION_EVENT)
    release = asyncio.Event()

    async def frame(*_args: Any) -> bytes:
        await release.wait()
        return b"jpg"

    bridge.manager.thumbnail_when_recorded = AsyncMock(side_effect=frame)
    await bridge.handle(review("new", objects=("dog",), rid="d1"))
    await bridge.handle(review("update", objects=("dog", "person"), zones=("porch",), rid="d1"))
    release.set()
    await hass.async_block_till_done()
    await bridge.handle(review("update", objects=("dog", "person"), rid="d1"))
    await hass.async_block_till_done()
    assert [(e.data["objects"], e.data["zones"]) for e in events] == [(["Person", "Animal"], ["porch"])]
    assert bridge._decided["d1"] == "person" and not bridge._announcing_latest


async def test_quiet_kind_joining_is_not_sent_again(hass: HomeAssistant, bridge: FrigateBridge) -> None:
    """A car is more than a dog, but with cars quiet it is no second alert, however often it is seen;
    a bicycle behind that quiet car still is."""
    bridge.quiet_kinds = {"Animal", "Car"}
    bridge.objects = bridge.objects | {"bicycle"}
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("dog",), rid="d1"))
    await hass.async_block_till_done()
    await bridge.handle(review("update", objects=("dog", "car"), rid="d1"))
    await bridge.handle(review("update", objects=("dog", "car"), rid="d1"))
    await hass.async_block_till_done()
    assert len(events) == 1
    await bridge.handle(review("update", objects=("dog", "car", "bicycle"), rid="d1"))
    await hass.async_block_till_done()
    await bridge.handle(review("end", objects=("dog", "car", "bicycle"), rid="d1", end=T + 9))
    await hass.async_block_till_done()
    assert [e.data["objects"] for e in events] == [["Animal"], ["Car", "Bicycle", "Animal"]]


async def test_second_notification_cut_short_is_sent_by_the_next_message(
    hass: HomeAssistant, bridge: FrigateBridge
) -> None:
    events = async_capture_events(hass, DETECTION_EVENT)
    await bridge.handle(review("new", objects=("dog",), rid="d1"))
    await hass.async_block_till_done()
    never = asyncio.Event()

    async def hang(*_args: Any) -> bytes:
        await never.wait()
        return b""

    bridge.manager.thumbnail_when_recorded = AsyncMock(side_effect=hang)
    await bridge.handle(review("update", objects=("dog", "person"), rid="d1"))
    await asyncio.sleep(0)
    for task in list(bridge._announcing):
        task.cancel()
    await hass.async_block_till_done()
    assert len(events) == 1 and "d1" not in bridge._not_yet and not bridge._announcing_latest
    bridge.manager.thumbnail_when_recorded = AsyncMock(return_value=b"jpg")
    await bridge.handle(review("update", objects=("dog", "person"), rid="d1"))
    await hass.async_block_till_done()
    assert [e.data["objects"] for e in events] == [["Animal"], ["Person", "Animal"]]


async def test_unreadable_decided_items_are_skipped(hass: HomeAssistant, bridge: FrigateBridge, hass_storage) -> None:
    """One odd item costs only itself, not the rest of the state."""
    hass_storage[f"{DOMAIN}.frigate.{bridge.entry_id}"] = {"version": 1, "key": "k", "data": {
        "decided": [5, None, ["x"], {"a": 1}, ["d1", "dog"], ["d2", 7]],
        "not_yet": [["n1", ["Animal"]]],
    }}
    with patch.object(mqtt_mod, "async_wait_for_mqtt_client", AsyncMock(return_value=True)), patch.object(
        mqtt_mod, "async_subscribe", AsyncMock(return_value=MagicMock())
    ):
        await bridge.start()
    try:
        assert bridge._decided == {"5": None, "d1": "dog", "d2": None}
        assert bridge._not_yet == {"n1": frozenset({"Animal"})}
    finally:
        bridge.stop()


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
