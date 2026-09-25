"""Setting up and unloading an entry."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry
from synology_ss_playback import SSAuthError, SSConnectionError, SSError

from custom_components.surveillance_station.const import CARD_FILENAME, DOMAIN
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant

from .conftest import SERIAL, USER_INPUT


async def test_setup_and_unload(hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is mock_client

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    mock_client.logout.assert_awaited_once()


async def test_card_registered_as_resource(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """The card is a Lovelace resource carrying the release as a cache buster."""
    version = json.loads((Path(__file__).parents[1] / "custom_components/surveillance_station/manifest.json").read_text())[
        "version"
    ]
    resources = hass.data[LOVELACE_DATA].resources
    urls = [r["url"] for r in resources.async_items()]
    assert urls == [f"/surveillance_station_static/{CARD_FILENAME}?v={version}"]


async def test_card_resource_follows_the_last_entry(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Removing the last entry removes the card; adding one back restores it."""
    resources = hass.data[LOVELACE_DATA].resources
    assert await hass.config_entries.async_remove(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert resources.async_items() == []

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT, unique_id=SERIAL)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(resources.async_items()) == 1


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.login.side_effect = SSAuthError("SYNO.API.Auth", "login", 400)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_unreachable_retries(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.login.side_effect = SSConnectionError("SYNO.API.Auth", "login", None)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_incomplete_info_retries(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """An SS answer without the serial is retried, not a permanent setup error."""
    mock_client.info.side_effect = SSError("SYNO.SurveillanceStation.Info", "GetInfo", None, "answer without a serial")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unique_id_migrated_to_serial(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Entries created before 0.5 were keyed by host:port."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="192.0.2.10:5000", data=USER_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.unique_id == SERIAL
