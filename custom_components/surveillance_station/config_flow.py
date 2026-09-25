"""Config flow: host + a dedicated DSM account."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SSAuthError, SSError, SurveillanceStationClient
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


class SurveillanceStationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}")
            self._abort_if_unique_id_configured()
            client = SurveillanceStationClient(
                async_get_clientsession(self.hass, verify_ssl=user_input[CONF_VERIFY_SSL]),
                user_input[CONF_HOST],
                user_input[CONF_PORT],
                user_input[CONF_SSL],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await client.login()
                cameras = await client.cameras()
            except SSAuthError:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, asyncio.TimeoutError):
                errors["base"] = "cannot_connect"
            except SSError:
                _LOGGER.exception("Surveillance Station rejected the setup calls")
                errors["base"] = "unknown"
            else:
                await client.logout()
                if not cameras:
                    errors["base"] = "no_cameras"
                else:
                    return self.async_create_entry(
                        title=f"Surveillance Station ({user_input[CONF_HOST]})", data=user_input
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input or {}),
            errors=errors,
        )
