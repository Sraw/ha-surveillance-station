"""The cameras' devices and what sits on them: entities' ids from 0.22 kept, the event and the sensor, the actions by device."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, flush_store
from synology_ss_playback import Camera, SSConnectionError

from custom_components.surveillance_station import async_remove_config_entry_device
from custom_components.surveillance_station.const import (
    CONF_FRIGATE,
    CONF_FRIGATE_LINK,
    CONF_FRIGATE_OBJECTS,
    CONF_FRIGATE_TOPIC,
    DOMAIN,
)
from custom_components.surveillance_station.device import (
    DEVICE_GONE_AFTER,
    camera_device_info,
    camera_id_of,
    camera_identifier,
    hub_identifier,
)
from custom_components.surveillance_station.event import DetectionEvent
from custom_components.surveillance_station.frigate import DATA_FRIGATE, FrigateBridge
from custom_components.surveillance_station.mute import WAITING_GONE_AFTER, MuteRule, MuteRules, store_key
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .test_mute import camera_devices, client, detect, frigate, listing, switches, turn  # noqa: F401 (fixtures)


def entity_of(hass: HomeAssistant, entry: MockConfigEntry, domain: str, unique: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{unique}")
    assert entity_id is not None, unique
    return entity_id


async def start(hass: HomeAssistant, entry: MockConfigEntry, **options) -> None:
    """Set up the entry (added already or not) with Frigate detections on."""
    if entry.entry_id not in {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}:
        entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_FRIGATE: True, CONF_FRIGATE_TOPIC: "frigate", CONF_FRIGATE_OBJECTS: ["person", "car", "dog"], **options},
    )
    with patch("custom_components.surveillance_station.FrigateBridge.start", AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_the_entry_and_each_camera_have_a_device(
    hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry  # noqa: F811
) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    devices = dr.async_get(hass)
    hub = devices.async_get_device_by_identifier(hub_identifier(mock_config_entry.entry_id), mock_config_entry.entry_id)
    assert hub is not None and hub.name == "Surveillance Station"
    cameras = camera_devices(hass, mock_config_entry)
    assert {i: d.name for i, d in cameras.items()} == {6: "Drive Way", 10: "Front Door"}
    assert all(d.via_device_id == hub.id and d.model == "Surveillance Station camera" for d in cameras.values())
    assert all(d.configuration_url is None for d in cameras.values())  # no dashboard to link to
    # Each entity is on its camera's device; the entry-wide switches are on the entry's.
    entities = er.async_get(hass)
    for camera_id, device in cameras.items():
        on_it = {e.unique_id.removeprefix(f"{mock_config_entry.entry_id}_") for e in er.async_entries_for_device(entities, device.id)}
        assert on_it == {
            f"mute_camera_id_{camera_id}", f"detection_{camera_id}", f"mute_ends_{camera_id}",
            *(f"mute_camera_kind_id_{camera_id}_{k}" for k in ("person", "car", "animal")),
        }
    assert {e.unique_id.removeprefix(f"{mock_config_entry.entry_id}_") for e in er.async_entries_for_device(entities, hub.id)} == {
        "mute_all", "mute_person", "mute_car", "mute_animal",
    }


async def test_a_camera_device_links_to_the_dashboard(hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock) -> None:  # noqa: F811
    await start(hass, mock_config_entry, **{CONF_FRIGATE_LINK: "/ss-playback/playback"})
    await hass.data[DATA_FRIGATE][mock_config_entry.entry_id].camera_names()
    await hass.async_block_till_done()
    urls = {i: d.configuration_url for i, d in camera_devices(hass, mock_config_entry).items()}
    assert urls == {i: f"homeassistant://ss-playback/playback?ss_camera={i}" for i in (6, 10)}


def test_a_camera_device_info_links_only_to_a_path_of_this_instance() -> None:
    def url(link: str) -> str | None:
        return camera_device_info("e", 6, "Cam", link).get("configuration_url")

    assert url("/lovelace/ss?tab=1") == "homeassistant://lovelace/ss?tab=1&ss_camera=6"
    assert url("") is None and url("//evil.example/x") is None and url("https://evil.example/x") is None and url("ss") is None


def test_only_a_camera_device_names_a_camera() -> None:
    def device(*identifiers: tuple[str, str]) -> MagicMock:
        return MagicMock(identifiers=set(identifiers))

    assert camera_id_of("e", device(camera_identifier("e", 12))) == 12
    assert camera_id_of("e", device(hub_identifier("e"))) is None
    assert camera_id_of("e", device(camera_identifier("other", 12))) is None
    assert camera_id_of("e", device(("elsewhere", "e_camera_12"))) is None
    assert camera_id_of("e", device((DOMAIN, "e_camera_x"))) is None


async def test_switches_of_0_22_keep_their_entities_and_become_the_cameras(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock  # noqa: F811
) -> None:
    """Their unique ids named the camera by its key: they move to its id, entity_id and all."""
    entry = mock_config_entry.entry_id
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)

    def old(unique: str, object_id: str) -> str:
        return registry.async_get_or_create(
            "switch", DOMAIN, f"{entry}_{unique}", config_entry=mock_config_entry, suggested_object_id=object_id
        ).entity_id

    drive, drive_car = old("mute_camera_driveway", "old_drive"), old("mute_camera_kind_driveway_car", "old_drive_car")
    front, kept = old("mute_camera_frontdoor", "old_front"), old("mute_camera_id_10", "new_front")  # both: no move
    nobody = old("mute_camera_garage", "old_garage")
    await start(hass, mock_config_entry)
    bridge = hass.data[DATA_FRIGATE][entry]
    await bridge.camera_names()
    await hass.async_block_till_done()
    assert entity_of(hass, mock_config_entry, "switch", "mute_camera_id_6") == drive
    assert entity_of(hass, mock_config_entry, "switch", "mute_camera_kind_id_6_car") == drive_car
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry}_mute_camera_driveway") is None
    # It is the camera's switch, on its device: turning it on mutes the camera.
    assert registry.async_get(drive).device_id == camera_devices(hass, mock_config_entry)[6].id
    await turn(hass, True, drive)
    assert bridge.mute.rules() == [MuteRule(6, None, None)] and hass.states.get(drive).attributes["camera"] == "Drive Way"
    # The one whose id has a switch already is left as it is, and so is one of a camera SS does not list:
    # each until it has been missing for two listings, half an hour apart.
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry}_mute_camera_frontdoor") == front
    assert entity_of(hass, mock_config_entry, "switch", "mute_camera_id_10") == kept
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry}_mute_camera_garage") == nobody
    await listing(hass, bridge, 1)
    assert registry.async_get(nobody) is not None
    await listing(hass, bridge, 4)
    assert registry.async_get(nobody) is None and registry.async_get(front) is None
    assert registry.async_get(kept) is not None and registry.async_get(drive) is not None
    assert DEVICE_GONE_AFTER == 1800


async def test_rules_of_0_22_become_the_cameras(hass: HomeAssistant, hass_storage, caplog) -> None:
    """Stored by camera key (version 1) until SS lists the cameras: then by id (version 2)."""
    old = [
        {"camera": "driveway", "kind": "Person", "until": None},
        {"camera": "frontdoor", "kind": None, "until": None, "excluded": ["Car"]},
        {"camera": "renamed", "kind": None, "until": None},
        {"camera": "driveway", "kind": "Animal", "until": time.time() - 5},
        {"camera": None, "kind": "Car", "until": None},
    ]
    hass_storage[store_key("e")] = {"version": 1, "key": store_key("e"), "data": {"rules": old}}
    rules = MuteRules(hass, "e")
    await rules.async_load()
    changes = []
    rules.async_add_listener(lambda: changes.append(1))
    # Waiting for their cameras: the rule for every camera works, and none is lost from the file.
    assert rules.rules() == [MuteRule(None, "Car", None)]
    rules.async_name_cameras({})  # SS answering oddly: the next listing decides
    assert not changes and len(rules._waiting) == 3
    rules.add(6, "Car", None)  # written meanwhile, the rules waiting with it
    await flush_store(rules._store)
    stored = hass_storage[store_key("e")]
    assert stored["version"] == 2
    assert [r.get("camera") for r in stored["data"]["rules"] if "camera" in r] == ["driveway", "frontdoor", "renamed"]
    # Listed: the rules are the cameras'; the one for a camera nobody has waits on, and is dropped, said,
    # once the listings have lacked it for half an hour.
    listed = {"driveway": 6, "frontdoor": 10}
    rules.async_name_cameras(listed)
    assert len(changes) == 2 and [w.camera for w in rules._waiting] == ["renamed"] and "Dropping" not in caplog.text
    assert set(rules.rules()) == {
        MuteRule(None, "Car", None), MuteRule(6, "Car", None), MuteRule(6, "Person", None), MuteRule(10, None, None, frozenset({"Car"})),
    }
    with patch("custom_components.surveillance_station.mute.time.time", return_value=time.time() + WAITING_GONE_AFTER - 5):
        rules.async_name_cameras(listed)
    assert len(rules._waiting) == 1
    with patch("custom_components.surveillance_station.mute.time.time", return_value=time.time() + WAITING_GONE_AFTER + 5):
        rules.async_name_cameras(listed)
    assert not rules._waiting and "Dropping the mute rule for 'renamed'" in caplog.text
    await flush_store(rules._store)
    assert all("camera" not in r and "camera_id" in r for r in hass_storage[store_key("e")]["data"]["rules"])
    before = len(changes)
    rules.async_name_cameras({"driveway": 6})  # nothing waiting: nothing changes
    assert len(changes) == before
    again = MuteRules(hass, "e")
    await again.async_load()
    assert set(again.rules()) == set(rules.rules())
    rules.stop()
    again.stop()


async def test_a_partial_first_listing_does_not_lose_the_rules_of_0_22(hass: HomeAssistant, hass_storage) -> None:
    old = [{"camera": "driveway", "kind": None, "until": None}, {"camera": "frontdoor", "kind": "Car", "until": None}]
    hass_storage[store_key("e")] = {"version": 1, "key": store_key("e"), "data": {"rules": old}}
    rules = MuteRules(hass, "e")
    await rules.async_load()
    rules.async_name_cameras({"driveway": 6})  # SS listed only some of its cameras
    assert rules.rules() == [MuteRule(6, None, None)] and [w.camera for w in rules._waiting] == ["frontdoor"]
    rules.async_name_cameras({"driveway": 6, "frontdoor": 10})
    assert set(rules.rules()) == {MuteRule(6, None, None), MuteRule(10, "Car", None)} and not rules._waiting
    rules.stop()


async def test_a_rule_of_0_22_does_not_replace_one_made_since(hass: HomeAssistant, hass_storage) -> None:
    hass_storage[store_key("e")] = {
        "version": 1, "key": store_key("e"), "data": {"rules": [{"camera": "driveway", "kind": None, "until": None}]},
    }
    rules = MuteRules(hass, "e")
    await rules.async_load()
    now = time.time()
    rules.add(6, None, now + 60)
    rules.async_name_cameras({"driveway": 6})
    assert rules.rules() == [MuteRule(6, None, now + 60)]
    rules.stop()


async def test_a_store_from_a_newer_version_is_not_read(hass: HomeAssistant, hass_storage, caplog) -> None:
    """0.22 refuses version 2 (its rules would read as rules for every camera); so does this one a later one."""
    hass_storage[store_key("e")] = {"version": 3, "key": store_key("e"), "data": {"rules": [{"camera_id": 6, "kind": None, "until": None}]}}
    rules = MuteRules(hass, "e")
    await rules.async_load()
    assert rules.rules() == [] and "unreadable mute rules" in caplog.text
    with pytest.raises(NotImplementedError):  # what no version there is asks to be converted
        await rules._store._async_migrate_func(0, 1, {})
    rules.stop()


async def test_0_22_cannot_read_the_rules_of_version_2(hass: HomeAssistant, hass_storage) -> None:
    """Else it would read their camera ids as rules for every camera."""
    rules = MuteRules(hass, "e")
    await rules.async_load()
    rules.add(6, None, None)
    await flush_store(rules._store)
    assert hass_storage[store_key("e")]["version"] == 2
    with pytest.raises(HomeAssistantError):
        await Store(hass, 1, store_key("e")).async_load()  # 0.22's
    rules.stop()


async def test_the_detection_of_a_camera_is_an_event_of_its_device(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry) -> None:  # noqa: F811
    await frigate.camera_names()
    await hass.async_block_till_done()
    drive = entity_of(hass, mock_config_entry, "event", "detection_6")
    front = entity_of(hass, mock_config_entry, "event", "detection_10")
    assert hass.states.get(drive).state == "unknown"
    assert hass.states.get(drive).attributes["device_class"] == "motion" and hass.states.get(drive).attributes["event_types"] == ["detection"]
    await detect(hass, frigate, objects=("person", "dog"))
    state = hass.states.get(drive)
    assert state.state != "unknown" and state.attributes["event_type"] == "detection"
    assert {k: state.attributes[k] for k in ("objects", "labels", "zones", "severity", "muted", "frigate_camera", "review_id")} == {
        "objects": ["Person", "Animal"], "labels": ["person", "dog"], "zones": [], "severity": "alert", "muted": False,
        "frigate_camera": "drive_way", "review_id": "1790000000.1-abc",
    }
    assert state.attributes["bookmark_id"] == 100 and "start" in state.attributes
    assert not {"image", "thumbnail", "url"} & state.attributes.keys()  # signed addresses stay out of the recorder
    assert hass.states.get(front).state == "unknown"
    # Muted: still an event, marked.
    frigate.mute.add(6, None, None)
    await detect(hass, frigate, rid="1790000000.2-abc")
    assert hass.states.get(drive).attributes["muted"] is True and hass.states.get(drive).attributes["review_id"] == "1790000000.2-abc"
    # Another entry's detection, or a camera SS does not list, is nothing of ours.
    before = hass.states.get(drive)
    hass.bus.async_fire("surveillance_station_detection", {"entry_id": "other", "camera_id": 6, "muted": False})
    hass.bus.async_fire("surveillance_station_detection", {"entry_id": mock_config_entry.entry_id, "camera_id": 99})
    await hass.async_block_till_done()
    assert hass.states.get(drive) == before and hass.states.get(front).state == "unknown"


def test_an_event_before_the_entity_is_added_is_nothing() -> None:
    event = DetectionEvent(MagicMock(entry_id="e"), 6, {})
    with patch.object(event, "_trigger_event") as trigger:
        event.detected({"objects": ["Person"]})
    trigger.assert_not_called()


async def test_the_sensor_says_when_a_camera_s_mute_ends(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry) -> None:  # noqa: F811
    await frigate.camera_names()
    await hass.async_block_till_done()
    sensor = entity_of(hass, mock_config_entry, "sensor", "mute_ends_6")
    other = entity_of(hass, mock_config_entry, "sensor", "mute_ends_10")

    def shown(entity: str = sensor) -> tuple[str, bool]:
        state = hass.states.get(entity)
        return state.state, state.attributes["forever"]

    assert hass.states.get(sensor).attributes["device_class"] == "timestamp" and shown() == ("unknown", False)
    now = time.time()
    await hass.services.async_call(DOMAIN, "mute", {"camera": "Drive Way", "duration": {"hours": 2}}, blocking=True)
    until, forever = shown()
    assert abs(dt_util.parse_datetime(until).timestamp() - (now + 7200)) < 5 and not forever
    assert shown(other) == ("unknown", False)
    # Everything muted longer lasts as long as that; until lifted, there is no end to tell.
    await hass.services.async_call(DOMAIN, "mute", {"duration": {"hours": 5}}, blocking=True)
    assert abs(dt_util.parse_datetime(shown()[0]).timestamp() - (now + 5 * 3600)) < 5
    await hass.services.async_call(DOMAIN, "unmute", {}, blocking=True)
    assert shown() == ("unknown", False)
    await turn(hass, True, entity_of(hass, mock_config_entry, "switch", "mute_camera_id_6"))
    assert shown() == ("unknown", True)


async def test_actions_by_camera_device(hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry) -> None:  # noqa: F811
    await frigate.camera_names()
    await hass.async_block_till_done()
    cameras = camera_devices(hass, mock_config_entry)
    hub = dr.async_get(hass).async_get_device_by_identifier(hub_identifier(mock_config_entry.entry_id), mock_config_entry.entry_id)
    rules = lambda: {(r.camera, r.kind) for r in frigate.mute.rules()}  # noqa: E731
    await hass.services.async_call(DOMAIN, "mute", {"device_id": cameras[6].id, "objects": ["person"]}, blocking=True)
    assert rules() == {(6, "Person")}
    # With a name too: both cameras.
    await hass.services.async_call(DOMAIN, "mute", {"device_id": cameras[6].id, "camera": "Front Door"}, blocking=True)
    assert rules() == {(6, "Person"), (6, None), (10, None)}
    await hass.services.async_call(DOMAIN, "unmute", {"device_id": cameras[10].id}, blocking=True)
    assert rules() == {(6, "Person"), (6, None)}
    await hass.services.async_call(DOMAIN, "unmute", {"device_id": cameras[6].id, "objects": "person"}, blocking=True)
    assert rules() == {(6, None)}
    # A camera nobody is called that, or a device that is none, is an error however good the other one is.
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "unmute", {"device_id": cameras[6].id, "camera": "garage"}, blocking=True)
    assert err.value.translation_key == "unknown_camera" and rules() == {(6, None)}
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "unmute", {"device_id": [cameras[6].id, hub.id], "camera": "Drive Way"}, blocking=True)
    assert err.value.translation_key == "not_a_camera_device" and rules() == {(6, None)}
    # Devices are a list (a target of an automation is one).
    await hass.services.async_call(DOMAIN, "unmute", {"device_id": [cameras[6].id, cameras[10].id]}, blocking=True)
    assert rules() == set()
    await hass.services.async_call(DOMAIN, "mute", {"device_id": [cameras[6].id, cameras[10].id], "objects": ["car"]}, blocking=True)
    assert rules() == {(6, "Car"), (10, "Car")}
    await hass.services.async_call(DOMAIN, "unmute", {}, blocking=True)
    assert rules() == set()
    # The entry's own device, or none, is no camera.
    for device_id in (hub.id, "nowhere"):
        for action in ("mute", "unmute"):
            with pytest.raises(ServiceValidationError) as err:
                await hass.services.async_call(DOMAIN, action, {"device_id": device_id}, blocking=True)
            assert err.value.translation_key == "not_a_camera_device"
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "mute", {"camera": "garage"}, blocking=True)
    assert err.value.translation_key == "unknown_camera"
    assert rules() == set()


async def test_only_a_camera_ss_no_longer_lists_can_be_deleted_by_hand(
    hass: HomeAssistant, frigate: FrigateBridge, mock_config_entry: MockConfigEntry  # noqa: F811
) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    devices = dr.async_get(hass)
    entry = mock_config_entry

    async def removable(device: dr.DeviceEntry) -> bool:
        return await async_remove_config_entry_device(hass, entry, device)

    cameras = camera_devices(hass, entry)
    hub = devices.async_get_device_by_identifier(hub_identifier(entry.entry_id), entry.entry_id)
    assert not await removable(hub) and not await removable(cameras[6])
    gone = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={camera_identifier(entry.entry_id, 99)}, name="Gone"
    )
    frigate.mute.add(99, None, None)
    frigate.mute.add(6, None, None)
    assert await removable(gone)
    assert frigate.mute.rules() == [MuteRule(6, None, None)]  # its rules go with it, the others stay
    # Frigate detections off: nothing lists the cameras any more, so the devices left behind can all go (the entry's, never).
    hass.config_entries.async_update_entry(entry, options={CONF_FRIGATE: False})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert DATA_FRIGATE not in hass.data or entry.entry_id not in hass.data[DATA_FRIGATE]
    assert await removable(gone) and await removable(cameras[6]) and not await removable(hub)


async def test_a_camera_device_is_not_deleted_before_ss_has_listed_its_cameras(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock  # noqa: F811
) -> None:
    """SS answering oddly (or not at all) yet is not a camera gone: its rules stay."""
    client.cameras.side_effect = SSConnectionError("x", "List", None)
    await start(hass, mock_config_entry)
    bridge = hass.data[DATA_FRIGATE][mock_config_entry.entry_id]
    assert await bridge.camera_names() == [] and bridge.camera_ids() == set()
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_config_entry.entry_id, identifiers={camera_identifier(mock_config_entry.entry_id, 6)}, name="Drive Way"
    )
    bridge.mute.add(6, None, None)
    assert not await async_remove_config_entry_device(hass, mock_config_entry, device)
    assert bridge.mute.rules() == [MuteRule(6, None, None)]


async def test_a_camera_deleted_by_hand_gets_its_entities_when_it_is_listed_again(
    hass: HomeAssistant, frigate: FrigateBridge, client: MagicMock, mock_config_entry: MockConfigEntry  # noqa: F811
) -> None:
    await frigate.camera_names()
    await hass.async_block_till_done()
    entry = mock_config_entry
    front = camera_devices(hass, entry)[10]
    frigate.mute.add(10, None, None)
    client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True)]
    await listing(hass, frigate, 1)  # one listing without it: kept, but the user may delete it
    assert await async_remove_config_entry_device(hass, entry, front)
    dr.async_get(hass).async_remove_device(front.id)
    await hass.async_block_till_done()
    assert set(camera_devices(hass, entry)) == {6} and frigate.mute.rules() == []
    entities = er.async_get(hass)
    assert not [e for e in er.async_entries_for_config_entry(entities, entry.entry_id) if e.unique_id.endswith(("_10", "_10_car"))]
    # SS lists it again: a device and every entity of its, working.
    client.cameras.return_value = [Camera(id=6, name="Drive Way", enabled=True), Camera(id=10, name="Front Door", enabled=True)]
    await listing(hass, frigate, 2)
    device = camera_devices(hass, entry)[10]
    for domain, unique in (("switch", "mute_camera_id_10"), ("switch", "mute_camera_kind_id_10_car"), ("event", "detection_10"), ("sensor", "mute_ends_10")):
        entity_id = entity_of(hass, entry, domain, unique)
        assert entities.async_get(entity_id).device_id == device.id and hass.states.get(entity_id) is not None
    assert frigate.mute.rules() == [] and hass.states.get(entity_of(hass, entry, "switch", "mute_camera_id_10")).state == "off"


async def test_two_cameras_with_one_name_keep_their_ids(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: MagicMock  # noqa: F811
) -> None:
    client.cameras.return_value = [Camera(id=6, name="Same", enabled=True), Camera(id=10, name="Same", enabled=True)]
    await start(hass, mock_config_entry)
    bridge = hass.data[DATA_FRIGATE][mock_config_entry.entry_id]
    await bridge.camera_names()
    await hass.async_block_till_done()
    assert bridge.camera_ids() == {6, 10} and set(camera_devices(hass, mock_config_entry)) == {6, 10}
