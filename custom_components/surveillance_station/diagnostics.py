"""Diagnostics: the entry's settings (credentials redacted), what SS reports, and playback state."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from synology_ss_playback import SSError

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import SurveillanceStationConfigEntry
from .views import DATA_MANAGER

TO_REDACT = {CONF_HOST, CONF_PASSWORD, CONF_USERNAME, "serial", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SurveillanceStationConfigEntry
) -> dict[str, Any]:
    client = entry.runtime_data
    surveillance_station: dict[str, Any]
    try:
        surveillance_station = {
            "info": asdict(await client.info()),
            "cameras": [asdict(c) for c in await client.cameras()],
        }
    except SSError as err:
        surveillance_station = {"error": str(err)}
    return async_redact_data(
        {
            "entry": {"unique_id": entry.unique_id, "data": dict(entry.data)},
            "surveillance_station": surveillance_station,
            "playback": hass.data[DATA_MANAGER].stats(),
        },
        TO_REDACT,
    )
