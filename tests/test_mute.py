"""Muting notifications: the rules, the event's flag, the actions, the switches and the notification's buttons."""

from __future__ import annotations

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
from synology_ss_playback import Bookmark, Camera, SSConnectionError

from custom_components.surveillance_station.const import CONF_FRIGATE, CONF_FRIGATE_OBJECTS, CONF_FRIGATE_TOPIC, DETECTION_EVENT, DOMAIN
from custom_components.surveillance_station.frigate import DATA_FRIGATE, FrigateBridge
from custom_components.surveillance_station.mute import MUTE_RULES_MAX, MuteRule, MuteRules, store_key
from custom_components.surveillance_station.mute_services import normalize_kind
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

T = 1_790_000_000


def test_rules_cover_each_kind() -> None:
    """A review is muted only when every kind in it is."""
    rules = MuteRules(MagicMock(), "e")
    rules._rules = [MuteRule(None, "Animal", None), MuteRule("driveway", "Person", None)]
    assert rules.is_muted("backyard", ["Animal"])
    assert not rules.is_muted("backyard", ["Person", "Animal"])  # the person is news
    assert rules.is_muted("driveway", ["Person", "Animal"])
    assert not rules.is_muted("backyard", ["Person"])
    assert not rules.is_muted("backyard", ["Car"])
    assert not rules.is_muted("backyard", [])  # kinds unknown: only "every kind" rules apply
    rules._rules = [MuteRule("backyard", None, None)]
    assert rules.is_muted("backyard", ["Car", "Person"]) and rules.is_muted("backyard", [])
    assert not rules.is_muted("driveway", ["Car"])
    rules._rules = [MuteRule(None, None, None)]
    assert rules.is_muted("anywhere", ["Bicycle"])


def test_ended_rules_do_not_count() -> None:
    rules = MuteRules(MagicMock(), "e")
    rules._rules = [MuteRule(None, None, T + 100)]
    assert rules.is_muted("c", ["Person"], now=T + 99)
    assert not rules.is_muted("c", ["Person"], now=T + 100)


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
    assert [(r.camera, r.kind) for r in rules._rules] == [(None, None)]
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


@pytest.fixture
async def frigate(hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock) -> FrigateBridge:
    """An entry with Frigate detections on (MQTT itself not started)."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_FRIGATE: True, CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_OBJECTS: ["person", "car", "dog"]}
    )
    with patch("custom_components.surveillance_station.FrigateBridge.start", AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    bridge = hass.data[DATA_FRIGATE][mock_config_entry.entry_id]
    bridge.manager.thumbnail_when_recorded = AsyncMock(return_value=b"jpg")
    return bridge


async def detect(hass: HomeAssistant, bridge: FrigateBridge, **kwargs) -> list:
    events = async_capture_events(hass, DETECTION_EVENT)
    with patch("custom_components.surveillance_station.frigate.time.time", return_value=T + 2):
        await bridge.handle(review(**kwargs))
    await hass.async_block_till_done()
    return events


async def test_event_says_when_muted(hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock) -> None:
    """Still bookmarked and announced, marked muted; the counter in diagnostics too."""
    frigate.mute.add("drivew" "ay", "Person", None)
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
    frigate._cameras_at = -1e9  # the list is due
    client.cameras.side_effect = SSConnectionError("x", "List", None)
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
    assert hass.states.get(cam).attributes["mute_ends"] == "forever"
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
    assert hass.states.get(everything).attributes["muted_until"] is None
    assert hass.states.get(everything).attributes["mute_ends"] == "forever"
    await turn(False, everything)
    assert hass.states.get(everything).attributes["mute_ends"] is None
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
    assert rules() == {("driveway", "Person"): None, ("driveway", "Animal"): pytest.approx(now + 7200, abs=5)}
    assert hass.states.get(cam).state == "off"
    assert hass.states.get(kind["animal"]).attributes["muted_until"] is not None
    assert hass.states.get(kind["animal"]).attributes["mute_ends"] not in (None, "forever")


async def test_button_for_an_unknown_camera_is_ignored(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry) -> None:
    await frigate.camera_names()
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SS_MUTE:{mock_config_entry.entry_id}:60:Front Door"})
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SS_MUTE:{mock_config_entry.entry_id}:60:nowhere"})
    await hass.async_block_till_done()
    assert [r.camera for r in frigate.mute.rules()] == ["frontdoor"]


async def test_cameras_without_a_key_get_no_switch(hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True), Camera(id=9, name="!!", enabled=True)]
    frigate._cameras_at = -1e9  # the list is due
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
