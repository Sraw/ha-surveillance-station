"""Config flow: host + a dedicated DSM account, with reauth and reconfigure.

The entry's unique ID is the NAS serial number, so it survives an IP or port
change and a reconfigure can't point an entry at a different NAS.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from synology_ss_playback import (
    SSAuthError,
    SSConnectionError,
    SSError,
    SSInfo,
    SurveillanceStationClient,
)
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_VERIFY_SSL, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SSL, default=False): bool,
        vol.Required(CONF_VERIFY_SSL, default=False): bool,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class SurveillanceStationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def _validate(self, data: Mapping[str, Any]) -> tuple[SSInfo | None, str | None]:
        """Log in, identify the NAS and list cameras; (info, None) or (None, error key)."""
        client = SurveillanceStationClient(
            async_get_clientsession(self.hass, verify_ssl=data[CONF_VERIFY_SSL]),
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_SSL],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
        )
        try:
            await client.login()
            info = await client.info()
            cameras = await client.cameras()
        except SSAuthError:
            return None, "invalid_auth"
        except SSConnectionError:
            return None, "cannot_connect"
        except SSError:
            _LOGGER.exception("Surveillance Station rejected the setup calls")
            return None, "unknown"
        finally:
            await client.logout()
        if not cameras:
            return None, "no_cameras"
        return info, None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await self._validate(user_input)
            if info is not None:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Surveillance Station ({info.hostname or user_input[CONF_HOST]})",
                    data=user_input,
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await self._validate({**entry.data, **user_input})
            if info is not None:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the address or the account of the same NAS."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await self._validate(user_input)
            if info is not None:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
            errors["base"] = error
        # The password is never shown back; everything else is prefilled.
        suggested = user_input or {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, suggested),
            errors=errors,
        )
