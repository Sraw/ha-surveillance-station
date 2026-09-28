"""Config flow: user, reauth and reconfigure steps."""

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from synology_ss_playback import SSAuthError, SSConnectionError, SSError, SSInfo

from custom_components.surveillance_station.const import CONF_VERIFY_SSL, DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from .conftest import SERIAL, USER_INPUT


async def test_user_flow(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Surveillance Station (The-NAS)"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == SERIAL
    # The validation session is closed again.
    mock_client.logout.assert_awaited()


async def test_user_flow_defaults_to_https(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """HTTPS on DSM's port for it, accepting DSM's self-signed certificate."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.10", CONF_USERNAME: "ha-ss", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {k: result["data"][k] for k in (CONF_PORT, CONF_SSL, CONF_VERIFY_SSL)} == {
        CONF_PORT: 5001, CONF_SSL: True, CONF_VERIFY_SSL: False,
    }


@pytest.mark.parametrize("port", [0, 65536])
async def test_user_flow_port_out_of_range(hass: HomeAssistant, mock_client: MagicMock, port: int) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_PORT: port})
    mock_client.login.assert_not_awaited()


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (SSAuthError("SYNO.API.Auth", "login", 400), "invalid_auth"),
        (SSAuthError("SYNO.API.Auth", "login", 403), "otp_required"),
        (SSAuthError("SYNO.API.Auth", "login", 406), "otp_required"),
        (SSConnectionError("SYNO.API.Auth", "login", None), "cannot_connect"),
        (SSError("SYNO.API.Auth", "login", 119), "unknown"),
    ],
)
async def test_user_flow_errors_recover(
    hass: HomeAssistant, mock_client: MagicMock, side_effect: Exception, error: str
) -> None:
    mock_client.login.side_effect = side_effect
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_old_surveillance_station(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """An SS without the Web APIs used: said at setup (before logging in), not as failures later."""
    mock_client.missing_apis.return_value = ["SYNO.SurveillanceStation.Event v5"]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "unsupported"}
    mock_client.login.assert_not_awaited()


async def test_user_flow_no_cameras(hass: HomeAssistant, mock_client: MagicMock) -> None:
    cameras = mock_client.cameras.return_value
    mock_client.cameras.return_value = []
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "no_cameras"}

    mock_client.cameras.return_value = cameras
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    # Same NAS under another address: still the same serial.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_HOST: "nas.local"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_client.login.side_effect = SSAuthError("SYNO.API.Auth", "login", 400)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new"


async def test_reauth_other_nas(hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    """The address now leads to another NAS: its password isn't stored for this one."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    mock_client.info.return_value = SSInfo(serial="OTHER", hostname="other", version="9", timezone="UTC")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert mock_config_entry.data[CONF_PASSWORD] == USER_INPUT[CONF_PASSWORD]


async def test_reconfigure(hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.20"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_HOST] == "192.0.2.20"


async def test_reconfigure_shows_what_the_entry_has(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """An entry set up over HTTP keeps it: the form is filled from the entry, not the new defaults."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    suggested = {str(key): key.description["suggested_value"] for key in result["data_schema"].schema if key.description}
    assert (suggested[CONF_PORT], suggested[CONF_SSL]) == (5000, False)
    assert CONF_PASSWORD not in suggested


async def test_reconfigure_other_nas(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    mock_client.info.return_value = SSInfo(serial="OTHER", hostname="other", version="9", timezone="UTC")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.99"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert mock_config_entry.data[CONF_HOST] == USER_INPUT[CONF_HOST]


async def test_reauth_of_a_loaded_entry_reloads_once(
    hass: HomeAssistant, mock_client: MagicMock, setup_integration: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """No update listener next to async_update_reload_and_abort (HA warns, and reloads twice)."""
    result = await setup_integration.start_reauth_flow(hass)
    logins = mock_client.login.await_count
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new"})
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    # One login to check the password, one for the reload.
    assert mock_client.login.await_count == logins + 2
    assert "update listener" not in caplog.text


async def test_reconfigure_errors_recover(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    mock_client.login.side_effect = SSConnectionError("SYNO.API.Auth", "login", None)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.20"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.20"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
