"""Setting up and unloading an entry."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from synology_ss_playback import SSAuthError, SSConnectionError, SSError, SSInfo

import custom_components.surveillance_station as ss
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
    # Logged out, so retries don't pile up DSM sessions.
    mock_client.logout.assert_awaited()


async def test_unique_id_migrated_to_serial(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Entries created before 0.5 were keyed by host:port."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="192.0.2.10:5000", data=USER_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.unique_id == SERIAL


async def test_stale_scratch_files_are_logged(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A crash can leave ffmpeg scratch files behind; async_setup sweeps them at startup."""
    caplog.set_level(logging.INFO, logger=ss.__name__)
    with patch.object(ss, "remove_stale_temp_files", return_value=3):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert "Removed 3 stale remux scratch files" in caplog.text


async def test_yaml_mode_lovelace_loads_the_card_as_extra_js(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """No storage-backed resource collection (YAML-mode dashboards): fall
    back to add_extra_js_url, and _register_card has nothing to update."""
    with (
        patch.object(ss, "_storage_resources", return_value=None),
        patch.object(ss, "add_extra_js_url") as add_js,
    ):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    add_js.assert_called_once()
    assert add_js.call_args.args[0] is hass
    assert add_js.call_args.args[1].startswith(f"/surveillance_station_static/{CARD_FILENAME}?v=")


async def test_card_resource_url_is_updated_on_a_version_change(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A resource already registered under an older version gets its URL updated in place."""
    resources = hass.data[LOVELACE_DATA].resources
    assert len(resources.async_items()) == 1
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    with patch.object(ss, "_version", return_value="9.9.9-test"):
        assert await hass.config_entries.async_setup(setup_integration.entry_id)
        await hass.async_block_till_done()
    items = resources.async_items()
    assert len(items) == 1  # updated, not duplicated
    assert items[0]["url"] == f"/surveillance_station_static/{CARD_FILENAME}?v=9.9.9-test"


async def test_removing_one_of_several_entries_keeps_the_card(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """The card's Lovelace resource is only dropped with the last entry."""
    entry_a = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, data=USER_INPUT)
    entry_a.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)

    # Added only once the domain is already set up, so it isn't auto-loaded
    # by the component_loaded event still carrying the first entry's client.info().
    mock_client.info.return_value = SSInfo(serial="OTHERSERIAL", hostname="other", version="9", timezone="UTC")
    entry_b = MockConfigEntry(domain=DOMAIN, unique_id="OTHERSERIAL", data=USER_INPUT)
    entry_b.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_b.entry_id)
    resources = hass.data[LOVELACE_DATA].resources

    assert await hass.config_entries.async_remove(entry_a.entry_id)
    await hass.async_block_till_done()
    assert len(resources.async_items()) == 1  # entry_b is still here


async def test_removing_the_last_entry_in_yaml_mode_lovelace(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """No storage resources to clean up: async_remove_entry just returns."""
    with patch.object(ss, "_storage_resources", return_value=None):
        assert await hass.config_entries.async_remove(setup_integration.entry_id)
        await hass.async_block_till_done()
