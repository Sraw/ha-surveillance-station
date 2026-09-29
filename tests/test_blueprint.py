"""The shipped notification blueprint: valid, and it notifies as documented."""

from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    async_mock_service,
    mock_integration,
    mock_platform,
)
import voluptuous as vol

from custom_components.surveillance_station.const import DETECTION_EVENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr, template
from homeassistant.setup import async_setup_component

BLUEPRINT = Path(__file__).parent.parent / "blueprints/automation/surveillance_station/detection_notification.yaml"
T = 1_790_000_000


def detection(**changes) -> dict:
    return {
        "entry_id": "e", "camera": "Drive Way", "camera_key": "driveway", "muted": False, "objects": ["Person", "Car"], "severity": "alert", "start": T, "review_id": "r1",
        "image": "/api/surveillance_station/thumbnail/e/6/1790000002-large.jpg?exp=1&sig=s",
        "url": "/ss-playback/playback?ss_camera=6&ss_time=1789999997",
        **changes,
    }


async def test_notification_blueprint(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    await hass.config.async_set_time_zone("UTC")
    target = tmp_path / "blueprints/automation/surveillance_station"
    target.mkdir(parents=True)
    shutil.copy(BLUEPRINT, target)
    phone_entry = MockConfigEntry(domain="mobile_app")
    phone_entry.add_to_hass(hass)
    phone = dr.async_get(hass).async_get_or_create(config_entry_id=phone_entry.entry_id, identifiers={("mobile_app", "p")})
    sent = async_mock_service(hass, "notify", "mobile_app_phone")
    # The companion app's device action, as mobile_app defines it (the real
    # one pulls in camera, conversation... not in the test venv): its schema,
    # rendered with the automation's variables, to the phone's notify service.

    async def notify(hass, config, variables, context):
        data = {k: template.render_complex(config[k], variables) for k in ("message", "title", "data") if k in config}
        await hass.services.async_call("notify", "mobile_app_phone", {"target": "hook", **data}, blocking=True)

    mock_integration(hass, MockModule("mobile_app"))
    mock_platform(
        hass,
        "mobile_app.device_action",
        SimpleNamespace(
            ACTION_SCHEMA=cv.DEVICE_ACTION_BASE_SCHEMA.extend(
                {
                    vol.Required("type"): "notify",
                    vol.Required("message"): cv.template,
                    vol.Optional("title"): cv.template,
                    vol.Optional("data"): cv.template_complex,
                }
            ),
            async_call_action_from_config=notify,
        ),
    )
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": "surveillance_station/detection_notification.yaml",
                    "input": {"notify_device": phone.id, "cameras": ["Drive Way"], "objects": ["Person"]},
                }
            }
        },
    )
    await hass.async_block_till_done()
    for data in (
        detection(camera="Backyard"),  # another camera
        detection(objects=["Animal"]),  # other objects
        detection(review_id="r4", muted=True),  # muted in the integration
        detection(url=None, review_id="r2", objects=["Car", "Person"]),
        detection(review_id="r3", severity="detection"),
    ):
        hass.bus.async_fire(DETECTION_EVENT, data)
        await hass.async_block_till_done()
    assert len(sent) == 2
    call = sent[0].data
    assert call["title"] == "Car, Person at Drive Way"
    assert call["message"] == "14:13:20"
    assert call["target"] == "hook"
    assert call["data"] == {
        "image": "/api/surveillance_station/thumbnail/e/6/1790000002-large.jpg?exp=1&sig=s",
        "tag": "r2", "group": "r2", "channel": "Detections", "notification_icon": "mdi:walk",
        "ttl": 0, "priority": "high",
        "actions": [
            {"action": "SS_MUTE:e:3600:", "title": "Mute all 1 h"},
            {"action": "SS_MUTE:e:3600:driveway", "title": "Mute Drive Way 1 h"},
        ],
        # no link: tapping opens the app
    }
    assert sent[1].data["data"] == {
        "image": "/api/surveillance_station/thumbnail/e/6/1790000002-large.jpg?exp=1&sig=s",
        "tag": "r3", "group": "r3", "channel": "Detections", "notification_icon": "mdi:walk",
        "ttl": 0, "priority": "high",
        "actions": [
            {"action": "SS_MUTE:e:3600:", "title": "Mute all 1 h"},
            {"action": "SS_MUTE:e:3600:driveway", "title": "Mute Drive Way 1 h"},
        ],
        "clickAction": "/ss-playback/playback?ss_camera=6&ss_time=1789999997",
        "url": "/ss-playback/playback?ss_camera=6&ss_time=1789999997",
    }
