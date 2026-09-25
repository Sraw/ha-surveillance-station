"""Diagnostics never contain credentials or the NAS's identity."""

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.core import HomeAssistant


async def test_diagnostics(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_client: ClientSessionGenerator
) -> None:
    diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)
    text = str(diag)
    for secret in ("secret", "ha-ss", "192.0.2.10", "2360TESTSERIAL"):
        assert secret not in text
    assert diag["entry"]["data"]["password"] == "**REDACTED**"
    assert diag["surveillance_station"]["info"]["version"] == "9.3.0-12143"
    assert [c["name"] for c in diag["surveillance_station"]["cameras"]] == ["Drive Way", "Backyard"]
    assert diag["playback"]["sessions"] == 0
    assert diag["frigate"] is None  # not enabled
