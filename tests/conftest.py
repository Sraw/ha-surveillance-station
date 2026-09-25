"""Fixtures: a mocked library client (never the integration's internals)."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from synology_ss_playback import Bookmark, Camera, RecordingInfo, SSInfo

from custom_components.surveillance_station.const import CONF_VERIFY_SSL, DOMAIN
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.core import HomeAssistant

SERIAL = "2360TESTSERIAL"
USER_INPUT = {
    CONF_HOST: "192.0.2.10",
    CONF_PORT: 5000,
    CONF_SSL: False,
    CONF_VERIFY_SSL: False,
    CONF_USERNAME: "ha-ss",
    CONF_PASSWORD: "secret",
}
T0 = 1_790_000_000


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """The SurveillanceStationClient both the flow and the entry create."""
    client = MagicMock()
    client.login = AsyncMock()
    client.logout = AsyncMock()
    client.info = AsyncMock(return_value=SSInfo(serial=SERIAL, hostname="The-NAS", version="9.3.0-12143"))
    client.cameras = AsyncMock(
        return_value=[Camera(id=6, name="Drive Way", enabled=True), Camera(id=7, name="Backyard", enabled=False)]
    )
    client.recordings = AsyncMock(
        return_value=[
            RecordingInfo(id=100, camera_id=6, start=T0, end=T0 + 1800, mount_id=1, live=False, hevc=True),
            RecordingInfo(id=101, camera_id=6, start=T0 + 1800, end=T0 + 3600, mount_id=1, live=False, hevc=True),
        ]
    )
    client.bookmarks = AsyncMock(
        return_value=[Bookmark(id=1, camera_id=6, name="person", comment="", start=T0 + 60, end=T0 + 70)]
    )
    client.on_auth_failed = None
    with (
        patch("custom_components.surveillance_station.SurveillanceStationClient", return_value=client),
        patch("custom_components.surveillance_station.config_flow.SurveillanceStationClient", return_value=client),
    ):
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, data=USER_INPUT, title="Surveillance Station (The-NAS)")


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> MockConfigEntry:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
