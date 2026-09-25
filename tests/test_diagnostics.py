"""Diagnostics never contain credentials or the NAS's identity."""

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.surveillance_station.const import CONF_FRIGATE, CONF_FRIGATE_TOPIC, DOMAIN
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


async def test_diagnostics_with_frigate(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client, hass_client: ClientSessionGenerator
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_config_entry.data, unique_id=mock_config_entry.unique_id,
        options={CONF_FRIGATE: True, CONF_FRIGATE_TOPIC: "nvr"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.surveillance_station.frigate.FrigateBridge.start", AsyncMock(return_value=True)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diag["frigate"]["topic"] == "nvr/reviews"
    assert diag["frigate"]["messages"] == 0 and diag["frigate"]["ignored"] == {}
    assert diag["frigate"]["last_error"] is None
