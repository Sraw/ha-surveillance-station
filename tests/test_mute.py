"""Muting notifications: the rules, the event's flag, the actions, the switches and the notification's buttons."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    flush_store,
)
from synology_ss_playback import Bookmark, Camera, SSConnectionError, SSError

from custom_components.surveillance_station import async_unload_entry
from custom_components.surveillance_station.const import CONF_FRIGATE, CONF_FRIGATE_OBJECTS, CONF_FRIGATE_TOPIC, DETECTION_EVENT, DOMAIN
from custom_components.surveillance_station.frigate import DATA_FRIGATE, FrigateBridge
from custom_components.surveillance_station.mute import MUTE_RULES_MAX, MuteRule, MuteRules, store_key
from custom_components.surveillance_station.mute_services import normalize_kind
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

T = 1_790_000_000


async def test_rules_cover_each_kind(hass: HomeAssistant) -> None:
    """A review is muted only when every kind in it is."""
    rules = MuteRules(hass, "e")
    rules.replace(lambda r: True, [MuteRule(None, "Animal", None), MuteRule("driveway", "Person", None)])
    assert rules.is_muted("backyard", ["Animal"])
    assert not rules.is_muted("backyard", ["Person", "Animal"])  # the person is news
    assert rules.is_muted("driveway", ["Person", "Animal"])
    assert not rules.is_muted("backyard", ["Person"])
    assert not rules.is_muted("backyard", ["Car"])
    assert not rules.is_muted("backyard", [])  # kinds unknown: only "every kind" rules apply
    rules.replace(lambda r: True, [MuteRule("backyard", None, None)])
    assert rules.is_muted("backyard", ["Car", "Person"]) and rules.is_muted("backyard", [])
    assert not rules.is_muted("driveway", ["Car"])
    rules.replace(lambda r: True, [MuteRule(None, None, None)])
    assert rules.is_muted("anywhere", ["Bicycle"])
    # Every kind but some: any other label is muted; with the kinds unknown, not.
    rules.replace(lambda r: True, [MuteRule("backyard", None, None, frozenset({"Car"}))])
    assert rules.is_muted("backyard", ["Package", "Person"]) and not rules.is_muted("backyard", ["Car"])
    assert not rules.is_muted("backyard", [])
    rules.stop()


async def test_ended_rules_do_not_count(hass: HomeAssistant) -> None:
    rules = MuteRules(hass, "e")
    now = time.time()
    rules.add(None, None, now + 100)
    assert rules.is_muted("c", ["Person"], now=now + 99)
    assert not rules.is_muted("c", ["Person"], now=now + 100)
    rules.stop()


async def test_coverage(hass: HomeAssistant) -> None:
    """Whether a scope is muted and until when: what is_muted says of each detection in it."""
    rules = MuteRules(hass, "e")
    now = time.time()

    def rule(camera, kind, hours=None, excluded=()) -> MuteRule:
        return MuteRule(camera, kind, None if hours is None else now + hours * 3600, frozenset(excluded))

    def given(*new: MuteRule) -> None:
        rules.replace(lambda r: True, list(new))

    # Alternatives: the longest wins, until lifted over any end.
    given(rule("c", None), rule(None, None, 1))
    assert rules.coverage("c", None) == (True, None) and rules.coverage("c", "Car") == (True, None)
    given(rule("c", None, 3), rule(None, None, 1))
    assert rules.coverage("c", None) == (True, now + 3 * 3600)
    # A camera's kinds alone never cover all of it (a label may be anything), nor
    # a camera's rules every camera.
    given(rule("c", "Person"), rule("c", "Car"), rule("c", "Animal"))
    assert rules.coverage("c", None) == (False, None) and rules.coverage("c", "Car") == (True, None)
    assert rules.coverage(None, "Car") == (False, None)
    # A kind left out and muted by a rule of its own: the whole lasts as long as both do.
    given(rule("c", None, 1, {"Car"}), rule("c", "Car", 2))
    assert rules.coverage("c", None) == (True, now + 3600)
    given(rule("c", None, 1, {"Car"}), rule("c", "Car", 2), rule(None, None, 3))
    assert rules.coverage("c", None) == (True, now + 3 * 3600)
    given(rule("c", None, None, {"Person"}), rule(None, "Person", 2))
    assert rules.coverage("c", None) == (True, now + 2 * 3600) and rules.coverage("d", None) == (False, None)
    given(rule(None, None, 5, {"Car"}), rule(None, "Car", 4))
    assert rules.coverage(None, None) == (True, now + 4 * 3600) and rules.coverage("c", "Car") == (True, now + 4 * 3600)
    given()
    assert rules.coverage(None, None) == (False, None)
    rules.stop()


def test_normalize_kind() -> None:
    assert [normalize_kind(x) for x in ("person", "Person", " CAR ", "dog", "Cat", "traffic light")] == [
        "Person", "Person", "Car", "Animal", "Animal", "Traffic light",
    ]


async def test_rules_end_and_are_kept(hass: HomeAssistant, hass_storage, freezer) -> None:
    rules = MuteRules(hass, "e")
    await rules.async_load()
    changes = []
    remove = rules.async_add_listener(lambda: changes.append(1))
    now = dt_util.utcnow().timestamp()
    rules.add("driveway", "Person", now + 60)
    rules.add("driveway", "Person", now + 120)  # replaces
    rules.add(None, None, None)
    assert [(r.camera, r.kind, r.until) for r in rules.rules()] == [("driveway", "Person", now + 120), (None, None, None)]
    await flush_store(rules._store)
    assert hass_storage[store_key("e")]["data"]["rules"][0] == {"camera": "driveway", "kind": "Person", "until": now + 120}
    # Survives a restart; the ended ones don't.
    hass_storage[store_key("e")]["data"]["rules"].append({"camera": "x", "kind": None, "until": now - 5})
    again = MuteRules(hass, "e")
    await again.async_load()
    assert len(again.rules()) == 2
    # The timer ends the rule and tells the listeners.
    before = len(changes)
    freezer.tick(timedelta(seconds=121))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(changes) == before + 1
    assert [(r.camera, r.kind) for r in rules.rules()] == [(None, None)]
    assert rules.remove(lambda r: r.kind is None) == 1 and rules.remove(lambda r: True) == 0
    remove()
    rules.stop()
    again.stop()


async def test_unreadable_rules_are_ignored(hass: HomeAssistant, hass_storage, caplog) -> None:
    hass_storage[store_key("e")] = {"version": 1, "key": store_key("e"), "data": {"rules": [{"until": "soon"}]}}
    rules = MuteRules(hass, "e")
    await rules.async_load()
    assert rules.rules() == [] and "unreadable mute rules" in caplog.text
    rules.stop()


def review(objects=("person",), camera="drive_way", rid="1790000000.1-abc") -> dict:
    return {
        "type": "new",
        "before": {}, "after": {
            "id": rid, "camera": camera, "severity": "alert", "start_time": T + 0.4, "end_time": None,
            "data": {"objects": list(objects), "detections": [], "zones": []},
        },
    }


@pytest.fixture
def client(mock_client: MagicMock) -> MagicMock:
    mock_client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True), Camera(id=10, name="Front Door", enabled=True)]

    async def create(camera_id, name, start, end, comment=""):
        return Bookmark(id=100, camera_id=camera_id, name=name, comment=comment, start=int(start), end=int(end))

    mock_client.create_bookmark = AsyncMock(side_effect=create)
    return mock_client


async def set_up(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    """Set up the entry with Frigate detections on (MQTT itself not started)."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_FRIGATE: True, CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_OBJECTS: ["person", "car", "dog"]}
    )
    with patch("custom_components.surveillance_station.FrigateBridge.start", AsyncMock(return_value=None)):
        ok = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return ok


@pytest.fixture
async def frigate(hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock) -> FrigateBridge:
    """An entry with Frigate detections on (MQTT itself not started)."""
    assert await set_up(hass, mock_config_entry)
    bridge = hass.data[DATA_FRIGATE][mock_config_entry.entry_id]
    bridge.manager.thumbnail_when_recorded = AsyncMock(return_value=b"jpg")
    return bridge


def switches(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    """The entry's switches' entity ids, by their unique id less the entry id."""
    return {
        e.unique_id.removeprefix(f"{entry.entry_id}_"): e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


async def turn(hass: HomeAssistant, on: bool, entity: str) -> None:
    await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": entity}, blocking=True)
    await hass.async_block_till_done()


def press(hass: HomeAssistant, entry: MockConfigEntry, seconds: int, camera: str = "") -> None:
    """A notification's mute button."""
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SS_MUTE:{entry.entry_id}:{seconds}:{camera}"})


async def listing(hass: HomeAssistant, bridge: FrigateBridge, later: int) -> None:
    """SS lists its cameras again: the later-th listing after the first (each one due after the one before)."""
    with patch("custom_components.surveillance_station.frigate._monotonic", return_value=time.monotonic() + 601 * later):
        await bridge.camera_names()
    await hass.async_block_till_done()


async def detect(hass: HomeAssistant, bridge: FrigateBridge, **kwargs) -> list:
    events = async_capture_events(hass, DETECTION_EVENT)
    with patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 2):
        await bridge.handle(review(**kwargs))
    await hass.async_block_till_done()
    return events


async def test_event_says_when_muted(hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock) -> None:
    """Still bookmarked and announced, marked muted; the counter in diagnostics too."""
    frigate.mute.add("driveway", "Person", None)
    [event] = await detect(hass, frigate)
    assert event.data["muted"] is True and event.data["camera_key"] == "driveway"
    client.create_bookmark.assert_awaited_once()
    assert frigate.stats()["announced"] == 1 and frigate.stats()["announced_muted"] == 1
    frigate.mute.remove(lambda r: True)
    [event] = await detect(hass, frigate, rid="1790000000.2-abc")
    assert event.data["muted"] is False
    assert frigate.stats()["announced_muted"] == 1


async def test_only_animals_muted_and_a_person_joins(hass: HomeAssistant, frigate: FrigateBridge) -> None:
    frigate.mute.add(None, "Animal", None)
    [event] = await detect(hass, frigate, objects=("dog",))
    assert event.data["muted"] is True
    [event] = await detect(hass, frigate, objects=("dog", "person"), rid="1790000000.2-abc")
    assert event.data["muted"] is False


async def test_mute_and_unmute_actions(hass: HomeAssistant, frigate: FrigateBridge) -> None:
    rules = frigate.mute
    now = dt_util.utcnow().timestamp()
    await hass.services.async_call(DOMAIN, "mute", {"duration": {"hours": 2}}, blocking=True)
    [rule] = rules.rules()
    assert (rule.camera, rule.kind) == (None, None) and 7199 < rule.until - now < 7201
    # A Frigate camera name works as well as the SS one; several objects, one rule each.
    await hass.services.async_call(DOMAIN, "mute", {"camera": "drive_way", "objects": ["person", "dog"]}, blocking=True)
    assert {(r.camera, r.kind) for r in rules.rules()} == {(None, None), ("driveway", "Person"), ("driveway", "Animal")}
    await hass.services.async_call(DOMAIN, "unmute", {"objects": "person"}, blocking=True)
    assert {(r.camera, r.kind) for r in rules.rules()} == {(None, None), ("driveway", "Animal")}
    await hass.services.async_call(DOMAIN, "unmute", {"camera": "Drive Way"}, blocking=True)
    assert {(r.camera, r.kind) for r in rules.rules()} == {(None, None)}
    await hass.services.async_call(DOMAIN, "unmute", {}, blocking=True)
    assert rules.rules() == []
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "mute", {"camera": "garage"}, blocking=True)


async def test_actions_without_frigate(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "mute", {}, blocking=True)


async def test_camera_when_ss_is_down_uses_the_list_it_had(hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock) -> None:
    await frigate.camera_names()
    client.cameras.side_effect = SSConnectionError("x", "List", None)
    with patch("custom_components.surveillance_station.frigate._monotonic", return_value=time.monotonic() + 601):  # the list is due
        await hass.services.async_call(DOMAIN, "mute", {"camera": "Front Door"}, blocking=True)
        assert [(r.camera, r.kind) for r in frigate.mute.rules()] == [("frontdoor", None)]
        assert await frigate.camera_names() == ["Drive Way", "Front Door"]


async def test_switches(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry, freezer) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    ids = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)}
    assert set(ids) == {f"{mock_config_entry.entry_id}_{k}" for k in (
        "mute_all", "mute_person", "mute_car", "mute_animal", "mute_camera_driveway", "mute_camera_frontdoor",
        *(f"mute_camera_kind_{c}_{k}" for c in ("driveway", "frontdoor") for k in ("person", "car", "animal")),
    )}
    everything = ids[f"{mock_config_entry.entry_id}_mute_all"]
    camera = ids[f"{mock_config_entry.entry_id}_mute_camera_driveway"]
    person = ids[f"{mock_config_entry.entry_id}_mute_person"]
    assert hass.states.get(everything).state == "off"
    # One kind on one camera: only that kind, only there.
    driveway_car = ids[f"{mock_config_entry.entry_id}_mute_camera_kind_driveway_car"]
    await hass.services.async_call("switch", "turn_on", {"entity_id": driveway_car}, blocking=True)
    assert hass.states.get(driveway_car).state == "on" and hass.states.get(camera).state == "off"
    assert frigate.mute.is_muted("driveway", ["Car"]) and not frigate.mute.is_muted("driveway", ["Person"])
    assert not frigate.mute.is_muted("frontdoor", ["Car"])
    await hass.services.async_call("switch", "turn_off", {"entity_id": driveway_car}, blocking=True)
    assert not frigate.mute.is_muted("driveway", ["Car"])
    await hass.services.async_call("switch", "turn_on", {"entity_id": camera}, blocking=True)
    assert hass.states.get(camera).state == "on" and hass.states.get(camera).attributes["muted_until"] is None
    assert hass.states.get(everything).state == "off"
    assert frigate.mute.is_muted("driveway", ["Person"]) and not frigate.mute.is_muted("frontdoor", ["Person"])
    # The scope is in the attributes, for the mute card.
    assert hass.states.get(everything).attributes["camera"] is None and hass.states.get(everything).attributes["kind"] is None
    assert hass.states.get(camera).attributes["camera"] == "Drive Way" and hass.states.get(camera).attributes["kind"] is None
    assert hass.states.get(driveway_car).attributes["camera"] == "Drive Way" and hass.states.get(driveway_car).attributes["kind"] == "car"
    assert hass.states.get(person).attributes["camera"] is None and hass.states.get(person).attributes["kind"] == "person"
    # A timed mute from the action shows on the switch too, and ends by itself.
    await hass.services.async_call(DOMAIN, "mute", {"objects": ["person"], "duration": {"minutes": 5}}, blocking=True)
    state = hass.states.get(person)
    assert state.state == "on" and state.attributes["muted_until"] is not None
    freezer.tick(timedelta(seconds=301))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(person).state == "off"
    await hass.services.async_call("switch", "turn_off", {"entity_id": camera}, blocking=True)
    assert frigate.mute.rules() == [] and hass.states.get(camera).state == "off"


async def test_notification_button(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry) -> None:
    entry = mock_config_entry.entry_id
    now = dt_util.utcnow().timestamp()
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SS_MUTE:{entry}:3600:"})
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SS_MUTE:{entry}:1800:frontdoor"})
    for bad in (f"SS_MUTE:{entry}:soon:", f"SS_MUTE:{entry}:0:", f"SS_MUTE:{entry}:99999999:", "SS_MUTE:other:60:", "SS_MUTE", "OTHER", 5):
        hass.bus.async_fire("mobile_app_notification_action", {"action": bad})
    await hass.async_block_till_done()
    rules = {(r.camera, r.kind): r.until for r in frigate.mute.rules()}
    assert set(rules) == {(None, None), ("frontdoor", None)}
    assert 3599 < rules[(None, None)] - now < 3601 and 1799 < rules[("frontdoor", None)] - now < 1801


async def test_rules_survive_a_reload_and_go_with_the_entry(
    hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry, hass_storage
) -> None:
    frigate.mute.add("driveway", None, None)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert hass_storage[store_key(mock_config_entry.entry_id)]["data"]["rules"] == [{"camera": "driveway", "kind": None, "until": None}]
    with patch("custom_components.surveillance_station.FrigateBridge.start", AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert [(r.camera, r.kind) for r in hass.data[DATA_FRIGATE][mock_config_entry.entry_id].mute.rules()] == [("driveway", None)]
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert store_key(mock_config_entry.entry_id) not in hass_storage


@pytest.mark.parametrize("duration", [{"seconds": 0}, {"days": 4_000_000}])
async def test_duration_must_be_sensible(hass: HomeAssistant, frigate: FrigateBridge, duration: dict) -> None:
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "mute", {"duration": duration}, blocking=True)
    assert frigate.mute.rules() == []


async def test_stored_rules_that_cannot_end_are_dropped(hass: HomeAssistant, hass_storage) -> None:
    hass_storage[store_key("e")] = {
        "version": 1, "key": store_key("e"),
        "data": {"rules": [
            {"camera": "a", "kind": None, "until": float("inf")},
            {"camera": "b", "kind": None, "until": 1e13},
            {"camera": "c", "kind": None, "until": None},
        ]},
    }
    rules = MuteRules(hass, "e")
    await rules.async_load()
    assert [r.camera for r in rules.rules()] == ["c"]
    rules.stop()


async def test_a_full_list_drops_timed_rules_first(hass: HomeAssistant) -> None:
    rules = MuteRules(hass, "e")
    rules.add("keep", None, None)
    now = dt_util.utcnow().timestamp()
    for i in range(MUTE_RULES_MAX + 3):
        rules.add(f"c{i}", None, now + 3600)
    assert len(rules.rules()) == MUTE_RULES_MAX
    assert any(r.camera == "keep" for r in rules.rules())
    rules.stop()
    rules.add("later", None, None)  # stopped: nothing
    assert not any(r.camera == "later" for r in rules.rules())


async def test_switches_follow_the_hierarchy(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    ids = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)}
    entry = mock_config_entry.entry_id
    everything, person = ids[f"{entry}_mute_all"], ids[f"{entry}_mute_person"]
    cam = ids[f"{entry}_mute_camera_driveway"]
    kind = {k: ids[f"{entry}_mute_camera_kind_driveway_{k}"] for k in ("person", "car", "animal")}

    async def turn(on: bool, entity: str) -> None:
        await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": entity}, blocking=True)
        await hass.async_block_till_done()

    def states(*entities: str) -> list[str]:
        return [hass.states.get(e).state for e in entities]

    # All kinds of a camera shows every kind on; a kind turned off leaves the others muted and all-kinds off.
    await turn(True, cam)
    assert states(cam, *kind.values()) == ["on"] * 4
    await turn(False, kind["car"])
    assert states(cam, *kind.values()) == ["off", "on", "off", "on"]
    assert frigate.mute.is_muted("driveway", ["Person", "Animal"]) and not frigate.mute.is_muted("driveway", ["Car"])
    # Every kind on shows all-kinds on; all-kinds off lifts them all.
    await turn(True, kind["car"])
    assert states(cam, *kind.values()) == ["on"] * 4
    await turn(False, cam)
    assert states(cam, *kind.values()) == ["off"] * 4 and frigate.mute.rules() == []
    # A kind muted for every camera shows on for each camera, locked.
    await turn(True, person)
    assert states(kind["person"], kind["car"]) == ["on", "off"]
    assert hass.states.get(kind["person"]).attributes["locked"] and not hass.states.get(kind["car"]).attributes["locked"]
    with pytest.raises(ServiceValidationError):
        await turn(False, kind["person"])
    assert states(cam) == ["off"]
    await turn(False, person)
    assert states(kind["person"]) == ["off"] and not hass.states.get(kind["person"]).attributes["locked"]
    # Everything muted: all the others show on but are locked, and are free when it is lifted.
    await turn(True, everything)
    assert states(person, cam, *kind.values()) == ["on"] * 5
    assert all(hass.states.get(e).attributes["locked"] for e in (person, cam, *kind.values()))
    assert hass.states.get(cam).attributes["muted_until"] is None and "mute_ends" not in hass.states.get(cam).attributes
    with pytest.raises(ServiceValidationError):
        await turn(True, cam)
    await turn(False, everything)
    assert states(person, cam, *kind.values()) == ["off"] * 5
    assert not any(hass.states.get(e).attributes["locked"] for e in (person, cam, *kind.values()))


async def test_switches_and_timed_mutes(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry, freezer) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    ids = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)}
    entry = mock_config_entry.entry_id
    everything, cam = ids[f"{entry}_mute_all"], ids[f"{entry}_mute_camera_driveway"]
    kind = {k: ids[f"{entry}_mute_camera_kind_driveway_{k}"] for k in ("person", "car", "animal")}

    async def turn(on: bool, entity: str) -> None:
        await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": entity}, blocking=True)
        await hass.async_block_till_done()

    def rules() -> dict:
        return {(r.camera, r.kind): r.until for r in frigate.mute.rules()}

    # Turning a switch on over a timed mute makes it last until turned off.
    await hass.services.async_call(DOMAIN, "mute", {"duration": {"hours": 1}}, blocking=True)
    assert hass.states.get(everything).attributes["muted_until"] is not None
    await turn(True, everything)
    assert hass.states.get(everything).state == "on" and hass.states.get(everything).attributes["muted_until"] is None
    await turn(False, everything)
    assert hass.states.get(everything).state == "off" and hass.states.get(everything).attributes["muted_until"] is None
    await hass.services.async_call(DOMAIN, "mute", {"camera": "drive_way", "duration": {"hours": 1}}, blocking=True)
    assert hass.states.get(kind["car"]).attributes["muted_until"] is not None
    await turn(True, kind["car"])
    assert hass.states.get(kind["car"]).attributes["muted_until"] is None
    await turn(False, cam)
    assert rules() == {}
    # Splitting a timed camera mute keeps its end, but never shortens a longer kind mute.
    now = time.time()
    await hass.services.async_call(DOMAIN, "mute", {"camera": "drive_way", "duration": {"hours": 2}}, blocking=True)
    await turn(True, kind["person"])
    await turn(False, kind["car"])
    assert rules() == {("driveway", "Person"): None, ("driveway", None): now + 7200}
    assert hass.states.get(cam).state == "off"
    assert hass.states.get(kind["animal"]).attributes["muted_until"] == dt_util.utc_from_timestamp(now + 7200).isoformat()
    assert hass.states.get(kind["person"]).attributes["muted_until"] is None
    assert hass.states.get(kind["car"]).state == "off"


async def test_button_for_an_unknown_camera_is_ignored(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry) -> None:
    await frigate.camera_names()
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SS_MUTE:{mock_config_entry.entry_id}:60:Front Door"})
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SS_MUTE:{mock_config_entry.entry_id}:60:nowhere"})
    await hass.async_block_till_done()
    assert [r.camera for r in frigate.mute.rules()] == ["frontdoor"]


async def test_cameras_without_a_key_get_no_switch(hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    client.cameras.return_value = [*client.cameras.return_value, Camera(id=9, name="!!", enabled=True)]
    with patch("custom_components.surveillance_station.frigate._monotonic", return_value=time.monotonic() + 601):  # the list is due
        await frigate.camera_names()
    await hass.async_block_till_done()
    ids = {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id)}
    assert {i for i in ids if "mute_camera" in i and "mute_camera_kind" not in i} == {f"{mock_config_entry.entry_id}_mute_camera_{k}" for k in ("driveway", "frontdoor")}
    assert await frigate.resolve_camera("!!") is None


async def test_switches_go_when_frigate_is_turned_off(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    assert hass.states.get("switch.surveillance_station_mute_all_notifications") is not None
    hass.config_entries.async_update_entry(mock_config_entry, options={CONF_FRIGATE: False})
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    # Gone (a state left for the registry's entities is "unavailable", not a live switch).
    left = [s for s in hass.states.async_all("switch") if s.entity_id.startswith("switch.surveillance_station_mute")]
    assert left and all(s.state == "unavailable" for s in left)
    assert mock_config_entry.entry_id not in hass.data.get(DATA_FRIGATE, {})


def iso(ts: float) -> str:
    return dt_util.utc_from_timestamp(ts).isoformat()


async def test_all_kinds_lasts_as_long_as_its_longest_cover(
    hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry, freezer
) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    ids = switches(hass, mock_config_entry)
    cam = ids["mute_camera_driveway"]
    now = time.time()

    def shown() -> tuple[str, str | None]:
        state = hass.states.get(cam)
        return state.state, state.attributes["muted_until"]

    await turn(hass, True, cam)
    await hass.services.async_call(DOMAIN, "mute", {"duration": {"hours": 1}}, blocking=True)
    assert shown() == ("on", None)  # the camera's own mute outlasts everything's hour
    await hass.services.async_call(DOMAIN, "mute", {"camera": "Drive Way", "duration": {"hours": 3}}, blocking=True)
    assert shown() == ("on", iso(now + 3 * 3600))
    await hass.services.async_call(DOMAIN, "mute", {"duration": {"hours": 5}}, blocking=True)
    assert shown() == ("on", iso(now + 5 * 3600))
    await turn(hass, False, ids["mute_all"])
    assert shown() == ("on", iso(now + 3 * 3600))


async def test_kinds_without_a_switch(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry) -> None:
    """A kind turned off on a muted camera leaves every other kind muted, those without a switch too;
    all kinds shows on only when every kind there is muted."""
    await frigate.camera_names()
    await hass.async_block_till_done()
    ids = switches(hass, mock_config_entry)
    cam = ids["mute_camera_driveway"]
    kind = {k: ids[f"mute_camera_kind_driveway_{k}"] for k in ("person", "car", "animal")}
    muted = frigate.mute.is_muted
    await turn(hass, True, cam)
    await turn(hass, False, kind["car"])
    assert muted("driveway", ["Bicycle"]) and muted("driveway", ["Package", "Person", "Animal"])
    assert not muted("driveway", ["Car"]) and hass.states.get(cam).state == "off"
    await turn(hass, False, kind["person"])
    assert muted("driveway", ["Animal", "Bicycle"]) and not muted("driveway", ["Person"]) and not muted("driveway", ["Car"])
    await turn(hass, False, cam)
    assert frigate.mute.rules() == []
    for entity in kind.values():
        await turn(hass, True, entity)
    assert [hass.states.get(e).state for e in (cam, *kind.values())] == ["off", "on", "on", "on"]
    assert not muted("driveway", ["Package"])


async def test_a_kind_muted_everywhere_completes_a_camera(
    hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry
) -> None:
    """Each switch shows on exactly when every detection in its scope is muted."""
    await frigate.camera_names()
    await hass.async_block_till_done()
    ids = switches(hass, mock_config_entry)
    cam, person = ids["mute_camera_driveway"], ids["mute_person"]
    kind = {k: ids[f"mute_camera_kind_driveway_{k}"] for k in ("person", "car", "animal")}

    def states() -> list[str]:
        return [hass.states.get(e).state for e in (cam, *kind.values())]

    await turn(hass, True, cam)
    await turn(hass, False, kind["person"])
    await turn(hass, True, person)
    assert states() == ["on"] * 4 and hass.states.get(kind["person"]).attributes["locked"]
    assert all(frigate.mute.is_muted("driveway", [k]) for k in ("Person", "Car", "Animal", "Bicycle"))
    # The camera's kinds each muted, but not every kind there: all kinds off.
    await turn(hass, False, person)
    await turn(hass, False, cam)
    await turn(hass, True, kind["car"])
    await turn(hass, True, kind["animal"])
    await turn(hass, True, person)
    assert states() == ["off", "on", "on", "on"] and not frigate.mute.is_muted("driveway", ["Bicycle"])


async def test_a_split_survives_a_restart(hass: HomeAssistant, hass_storage, caplog) -> None:
    rules = MuteRules(hass, "e")
    await rules.async_load()
    rules.add("driveway", None, None)
    rules.replace(lambda r: False, [MuteRule("driveway", None, None, frozenset({"Car"})), MuteRule(None, "Car", None)])
    await flush_store(rules._store)
    assert hass_storage[store_key("e")]["data"]["rules"] == [
        {"camera": "driveway", "kind": None, "until": None, "excluded": ["Car"]},
        {"camera": None, "kind": "Car", "until": None},
    ]
    again = MuteRules(hass, "e")
    await again.async_load()
    assert again.rules() == rules.rules()
    assert again.is_muted("driveway", ["Bicycle"]) and again.is_muted("backyard", ["Car"])
    assert not again.is_muted("backyard", ["Bicycle"])
    # A kind's rule leaves nothing out; an "excluded" that is not a list makes the rules unreadable.
    hass_storage[store_key("f")] = {
        "version": 1, "key": store_key("f"), "data": {"rules": [{"camera": "a", "kind": "Car", "until": None, "excluded": ["Car"]}]},
    }
    third = MuteRules(hass, "f")
    await third.async_load()
    assert third.rules() == [MuteRule("a", "Car", None)]
    hass_storage[store_key("g")] = {
        "version": 1, "key": store_key("g"), "data": {"rules": [{"camera": "a", "kind": None, "until": None, "excluded": "Car"}]},
    }
    fourth = MuteRules(hass, "g")
    await fourth.async_load()
    assert fourth.rules() == [] and "unreadable mute rules" in caplog.text
    for r in (rules, again, third, fourth):
        r.stop()


async def test_stored_rules_are_one_per_camera_and_kind_and_bounded(hass: HomeAssistant, hass_storage) -> None:
    now = time.time()
    stored = [{"camera": "a", "kind": None, "until": None}, {"camera": "a", "kind": None, "until": now + 60}]
    stored += [{"camera": f"c{i}", "kind": None, "until": None} for i in range(MUTE_RULES_MAX + 5)]
    hass_storage[store_key("e")] = {"version": 1, "key": store_key("e"), "data": {"rules": stored}}
    rules = MuteRules(hass, "e")
    await rules.async_load()
    assert len(rules.rules()) == MUTE_RULES_MAX and rules.rules()[0] == MuteRule("a", None, None)
    assert [r.camera for r in rules.rules()].count("a") == 1
    rules.stop()


async def test_muting_several_objects_is_one_change(hass: HomeAssistant, frigate: FrigateBridge) -> None:
    changes = []
    frigate.mute.async_add_listener(lambda: changes.append(1))
    await hass.services.async_call(DOMAIN, "mute", {"camera": "drive_way", "objects": ["person", "car", "dog"]}, blocking=True)
    assert len(changes) == 1
    assert {(r.camera, r.kind) for r in frigate.mute.rules()} == {("driveway", "Person"), ("driveway", "Car"), ("driveway", "Animal")}
    # The same again changes nothing: not saved, no switch written.
    await hass.services.async_call(DOMAIN, "mute", {"camera": "drive_way", "objects": ["person", "car", "dog"]}, blocking=True)
    frigate.mute.remove(lambda r: r.camera == "nowhere")
    assert len(changes) == 1


async def test_unmute_a_camera_s_objects(hass: HomeAssistant, frigate: FrigateBridge) -> None:
    await hass.services.async_call(DOMAIN, "mute", {"camera": "drive_way", "objects": ["person", "car"]}, blocking=True)
    await hass.services.async_call(DOMAIN, "mute", {"objects": ["person"]}, blocking=True)
    await hass.services.async_call(DOMAIN, "mute", {"camera": "Front Door", "objects": ["person"]}, blocking=True)
    await hass.services.async_call(DOMAIN, "unmute", {"camera": "Drive Way", "objects": ["person"]}, blocking=True)
    assert {(r.camera, r.kind) for r in frigate.mute.rules()} == {("driveway", "Car"), (None, "Person"), ("frontdoor", "Person")}


async def test_a_notification_button_never_shortens_a_mute(
    hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry, freezer
) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    ids = switches(hass, mock_config_entry)
    now = time.time()

    async def pressed(seconds: int, camera: str = "") -> dict:
        press(hass, mock_config_entry, seconds, camera)
        await hass.async_block_till_done()
        return {(r.camera, r.kind): r.until for r in frigate.mute.rules()}

    await turn(hass, True, ids["mute_all"])
    assert await pressed(3600) == {(None, None): None}
    await turn(hass, False, ids["mute_all"])
    await hass.services.async_call(DOMAIN, "mute", {"camera": "Front Door", "duration": {"hours": 3}}, blocking=True)
    assert await pressed(1800, "frontdoor") == {("frontdoor", None): now + 3 * 3600}
    assert await pressed(4 * 3600, "frontdoor") == {("frontdoor", None): now + 4 * 3600}
    # A camera with a kind turned off: that kind is muted for the button's time, the rest stays as it was.
    await turn(hass, True, ids["mute_camera_driveway"])
    await turn(hass, False, ids["mute_camera_kind_driveway_car"])
    rules = await pressed(3600, "driveway")
    assert rules[("driveway", None)] is None and rules[("driveway", "Car")] == now + 3600
    assert [r.excluded for r in frigate.mute.rules() if (r.camera, r.kind) == ("driveway", None)] == [frozenset({"Car"})]
    assert (await pressed(1800, "driveway"))[("driveway", "Car")] == now + 3600
    # The mute action sets the end it is given, shorter or not.
    await hass.services.async_call(DOMAIN, "mute", {"camera": "Front Door", "duration": {"hours": 1}}, blocking=True)
    assert {(r.camera, r.kind): r.until for r in frigate.mute.rules()}[("frontdoor", None)] == now + 3600


async def test_a_camera_gone_from_ss_loses_its_switches_not_its_rules(
    hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    front = switches(hass, mock_config_entry)["mute_camera_frontdoor"]
    frigate.mute.add("frontdoor", None, None)
    client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True), Camera(id=10, name="Front Porch", enabled=True)]
    await listing(hass, frigate, 1)
    # Missing from one listing: kept (that listing may have been short).
    ids = switches(hass, mock_config_entry)
    assert "mute_camera_frontdoor" in ids and "mute_camera_kind_frontdoor_car" in ids and "mute_camera_frontporch" in ids
    assert hass.states.get(front).state == "on"
    await listing(hass, frigate, 2)
    ids = switches(hass, mock_config_entry)
    assert not any("frontdoor" in i for i in ids) and "mute_camera_frontporch" in ids and "mute_camera_kind_frontporch_car" in ids
    assert "mute_camera_driveway" in ids and hass.states.get(front) is None
    assert [r.camera for r in frigate.mute.rules()] == ["frontdoor"]
    # Its rule is lifted by its old name, or its key.
    await hass.services.async_call(DOMAIN, "unmute", {"camera": "Front Door"}, blocking=True)
    assert frigate.mute.rules() == []
    frigate.mute.add("frontdoor", "Car", None)
    await hass.services.async_call(DOMAIN, "unmute", {"camera": "frontdoor"}, blocking=True)
    assert frigate.mute.rules() == []
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "unmute", {"camera": "Front Door"}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "mute", {"camera": "Front Door"}, blocking=True)
    # Back in SS: its switches again at once; Front Porch's go after two listings without it.
    client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True), Camera(id=10, name="Front Door", enabled=True)]
    await listing(hass, frigate, 3)
    ids = switches(hass, mock_config_entry)
    assert "mute_camera_frontdoor" in ids and "mute_camera_frontporch" in ids
    assert hass.states.get(ids["mute_camera_kind_frontdoor_person"]).state == "off"
    await listing(hass, frigate, 4)
    ids = switches(hass, mock_config_entry)
    assert "mute_camera_frontdoor" in ids and "mute_camera_frontporch" not in ids


async def test_a_short_listing_once_or_one_without_cameras_removes_no_switch(
    hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The library skips cameras it cannot read and lists none when SS's answer has none."""
    await frigate.camera_names()
    await hass.async_block_till_done()
    before = switches(hass, mock_config_entry)
    client.cameras.return_value = []
    await listing(hass, frigate, 1)
    await listing(hass, frigate, 2)
    assert switches(hass, mock_config_entry) == before
    assert all(hass.states.get(e).state == "off" for i, e in before.items() if i.startswith("mute_camera"))
    client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True)]
    await listing(hass, frigate, 3)
    assert switches(hass, mock_config_entry) == before
    await listing(hass, frigate, 4)
    ids = switches(hass, mock_config_entry)
    assert not any("frontdoor" in i for i in ids) and "mute_camera_kind_driveway_car" in ids


async def test_switches_come_once_ss_answers(hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock) -> None:
    """SS down when the entry starts: the camera switches appear once it answers."""
    client.cameras.side_effect = [SSConnectionError("x", "List", None), client.cameras.return_value]
    assert await set_up(hass, mock_config_entry)
    ids = switches(hass, mock_config_entry)
    assert "mute_all" in ids and not any(i.startswith("mute_camera") for i in ids)
    for _ in range(5):  # a minute later (once the wait has begun: the listing runs as its own task)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
        await hass.async_block_till_done()
        if client.cameras.await_count == 2:
            break
    await hass.async_block_till_done()
    ids = switches(hass, mock_config_entry)
    assert client.cameras.await_count == 2 and "mute_camera_driveway" in ids and "mute_camera_kind_frontdoor_person" in ids
    assert hass.states.get(ids["mute_camera_driveway"]).state == "off"


async def test_ss_down_is_asked_less_and_less_often(hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock) -> None:
    client.cameras.side_effect = SSConnectionError("x", "List", None)
    waits = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 6:
            client.cameras.side_effect = None

    with patch("custom_components.surveillance_station.switch.asyncio", MagicMock(sleep=sleep)):
        assert await set_up(hass, mock_config_entry)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert waits == [60, 120, 240, 480, 600, 600]
    assert "mute_camera_driveway" in switches(hass, mock_config_entry)


async def test_platforms_that_cannot_unload_keep_the_bridge_running(
    hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry
) -> None:
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        assert not await async_unload_entry(hass, mock_config_entry)
    assert hass.data[DATA_FRIGATE][mock_config_entry.entry_id] is frigate and not frigate.stopped
    frigate.mute.add(None, None, None)
    assert frigate.mute.rules() == [MuteRule(None, None, None)]
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert frigate.stopped


async def test_cameras_listed_while_unloading_get_no_switch(
    hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    client.cameras.return_value = [*client.cameras.return_value, Camera(id=11, name="Garage", enabled=True)]
    unload = hass.config_entries.async_unload_platforms

    async def listed_meanwhile(entry, platforms):
        with patch("custom_components.surveillance_station.frigate._monotonic", return_value=time.monotonic() + 601):
            await frigate.camera_names()
        return await unload(entry, platforms)

    with patch.object(hass.config_entries, "async_unload_platforms", listed_meanwhile):
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    ids = switches(hass, mock_config_entry)
    assert "mute_camera_driveway" in ids and not any("garage" in i for i in ids)


async def test_a_failed_setup_leaves_no_mute_timer(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock, hass_storage, freezer
) -> None:
    stored = [{"camera": "a", "kind": None, "until": time.time() + 60}]
    key = store_key(mock_config_entry.entry_id)
    hass_storage[key] = {"version": 1, "key": key, "data": {"rules": stored}}
    with patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=RuntimeError("boom"))):
        assert not await set_up(hass, mock_config_entry)
    # A timer left running would end the rule and write the rules without it.
    for seconds in (61, 5):
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert hass_storage[key]["data"]["rules"] == stored


async def test_a_failed_setup_takes_the_bridge_away_too(hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock) -> None:
    """Its rules are stopped, so the mute actions must not find it and report a mute that does nothing."""
    with patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=RuntimeError("boom"))):
        assert not await set_up(hass, mock_config_entry)
    assert mock_config_entry.entry_id not in hass.data.get(DATA_FRIGATE, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "mute", {}, blocking=True)


async def test_ss_failing_to_list_cameras_is_not_asked_again_at_once(hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock) -> None:
    await frigate.camera_names()
    client.cameras.side_effect = SSConnectionError("x", "List", None)
    asked = client.cameras.await_count
    later = time.monotonic() + 601  # the list is due
    with patch("custom_components.surveillance_station.frigate._monotonic", return_value=later):
        for _ in range(3):  # a review each time: it fails as it did, without another listing
            with pytest.raises(SSError):
                await frigate.ss_camera("drive_way")
        assert client.cameras.await_count == asked + 1
        assert await frigate.resolve_camera("Drive Way") == "driveway"  # the list as it was
        assert client.cameras.await_count == asked + 1
    client.cameras.side_effect = None
    with patch("custom_components.surveillance_station.frigate._monotonic", return_value=later + 61):  # a minute on: asked again
        assert await frigate.ss_camera("drive_way") == (6, "Drive Way")
        assert client.cameras.await_count == asked + 2


async def test_cameras_are_resolved_in_every_entry_at_once(hass: HomeAssistant, frigate: FrigateBridge) -> None:
    other_asked = asyncio.Event()

    async def other_resolve(name: str) -> str | None:
        other_asked.set()
        return None

    resolve = frigate.resolve_camera

    async def after_the_other(name: str) -> str | None:
        await other_asked.wait()
        return await resolve(name)

    hass.data[DATA_FRIGATE]["other"] = MagicMock(mute=MuteRules(hass, "other"), resolve_camera=other_resolve)
    try:
        with patch.object(frigate, "resolve_camera", after_the_other):
            async with asyncio.timeout(5):
                await hass.services.async_call(DOMAIN, "mute", {"camera": "Drive Way"}, blocking=True)
    finally:
        del hass.data[DATA_FRIGATE]["other"]
    assert frigate.mute.rules() == [MuteRule("driveway", None, None)]


async def test_a_camera_off_says_its_rules_still_mute_other_kinds(
    hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry
) -> None:
    """Kinds without a switch muted on a camera shown off: others_muted, until all kinds is turned on then off, or unmute."""
    await frigate.camera_names()
    await hass.async_block_till_done()
    ids = switches(hass, mock_config_entry)
    cam = ids["mute_camera_driveway"]
    kind = {k: ids[f"mute_camera_kind_driveway_{k}"] for k in ("person", "car", "animal")}

    def others(entity: str = cam) -> bool:
        return hass.states.get(entity).attributes["others_muted"]

    assert not others()
    await turn(hass, True, cam)
    assert not others()  # on: nothing hidden
    for entity in kind.values():
        await turn(hass, False, entity)
    assert [hass.states.get(e).state for e in (cam, *kind.values())] == ["off"] * 4
    assert frigate.mute.is_muted("driveway", ["Bicycle"]) and others() and not others(ids["mute_camera_frontdoor"])
    await turn(hass, True, cam)
    await turn(hass, False, cam)
    assert frigate.mute.rules() == [] and not others()
    # The action for objects without a switch; unmute lifts them.
    await hass.services.async_call(DOMAIN, "mute", {"camera": "drive_way", "objects": ["bicycle"]}, blocking=True)
    await hass.async_block_till_done()
    assert others()
    await hass.services.async_call(DOMAIN, "unmute", {"camera": "drive_way"}, blocking=True)
    await hass.async_block_till_done()
    assert not others()
    # A kind with a switch shows on its own.
    await turn(hass, True, kind["car"])
    assert not others()
    assert "others_muted" not in hass.states.get(kind["car"]).attributes
    assert "others_muted" not in hass.states.get(ids["mute_all"]).attributes


async def test_a_button_for_a_camera_without_a_key_is_ignored(
    hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock, mock_config_entry: MockConfigEntry, caplog
) -> None:
    """Such a camera has no button; a made-up one must not mute a camera no switch shows."""
    client.cameras.return_value = [*client.cameras.return_value, Camera(id=9, name="!!", enabled=True)]
    await listing(hass, frigate, 1)
    assert "" in frigate.camera_keys()
    caplog.set_level("DEBUG", logger="custom_components.surveillance_station.mute_services")
    press(hass, mock_config_entry, 3600, "!!")
    await hass.async_block_till_done()
    assert frigate.mute.rules() == [] and "its camera has no key" in caplog.text


async def test_the_same_rule_again_is_no_change(hass: HomeAssistant) -> None:
    """Not saved, rescheduled or told, though replacing it would move it to the end."""
    rules = MuteRules(hass, "e")
    changes = []
    rules.async_add_listener(lambda: changes.append(1))
    rules.add("driveway", None, None)
    rules.add(None, "Person", None)
    rules.add("driveway", None, None)
    rules.replace(lambda r: r.kind == "Person", [MuteRule(None, "Person", None)])
    assert len(changes) == 2 and rules.rules() == [MuteRule("driveway", None, None), MuteRule(None, "Person", None)]
    rules.add("driveway", None, time.time() + 60)
    assert len(changes) == 3
    rules.stop()
