"""Config flow: host + a dedicated DSM account, with reauth and reconfigure.
Options: Frigate detections as bookmarks.

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

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import (
    CONF_FRIGATE,
    CONF_FRIGATE_OBJECTS,
    CONF_FRIGATE_LINK,
    CONF_FRIGATE_TOPIC,
    CONF_VERIFY_SSL,
    DEFAULT_FRIGATE_OBJECTS,
    DEFAULT_FRIGATE_TOPIC,
    DEFAULT_PORT,
    DOMAIN,
)

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
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FRIGATE, default=False): bool,
        vol.Required(CONF_FRIGATE_TOPIC, default=DEFAULT_FRIGATE_TOPIC): str,
        vol.Required(CONF_FRIGATE_OBJECTS, default=DEFAULT_FRIGATE_OBJECTS): SelectSelector(
            SelectSelectorConfig(
                options=["person", "car", "dog", "cat", "bicycle", "motorcycle", "bird", "horse", "package"],
                multiple=True,
                custom_value=True,
            )
        ),
        vol.Optional(CONF_FRIGATE_LINK): str,
    }
)


class SurveillanceStationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        return SurveillanceStationOptionsFlow()

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


class SurveillanceStationOptionsFlow(OptionsFlowWithReload):
    """Frigate detections as bookmarks (saving reloads the entry)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            topic = user_input[CONF_FRIGATE_TOPIC].strip().strip("/")
            link = (user_input.get(CONF_FRIGATE_LINK) or "").strip()
            objects = sorted({o.strip().lower() for o in user_input[CONF_FRIGATE_OBJECTS] if o.strip()})
            if not topic or any(c in topic for c in "#+"):
                errors[CONF_FRIGATE_TOPIC] = "invalid_topic"
            elif not objects:
                errors[CONF_FRIGATE_OBJECTS] = "no_objects"
            elif link and (not link.startswith("/") or link.startswith("//") or "\\" in link):
                errors[CONF_FRIGATE_LINK] = "invalid_link"
            else:
                return self.async_create_entry(
                    data={**user_input, CONF_FRIGATE_TOPIC: topic, CONF_FRIGATE_OBJECTS: objects, CONF_FRIGATE_LINK: link}
                )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, user_input or self.config_entry.options),
            errors=errors,
        )
