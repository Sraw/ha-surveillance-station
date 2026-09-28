"""Diagnostics: the entry's settings (credentials and addresses redacted), what SS reports, playback and Frigate state."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from synology_ss_playback import SSError

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import SurveillanceStationConfigEntry
from .const import CONF_FRIGATE_LINK, CONF_FRIGATE_URL
from .frigate import DATA_FRIGATE
from .views import DATA_MANAGER

# Diagnostics end up in bug reports: no credentials, nor names or addresses of the NAS, Frigate or a dashboard.
TO_REDACT = {
    CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_FRIGATE_LINK, CONF_FRIGATE_URL, "hostname", "serial", "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SurveillanceStationConfigEntry
) -> dict[str, Any]:
    surveillance_station: dict[str, Any]
    # Not loaded (setup failed or retrying): exactly when diagnostics are
    # wanted, so they say that rather than fail.
    if (client := getattr(entry, "runtime_data", None)) is None:
        surveillance_station = {"error": f"entry not loaded ({entry.state.value})"}
    else:
        try:
            surveillance_station = {
                "info": asdict(await client.info()),
                "cameras": [asdict(c) for c in await client.cameras()],
            }
        except SSError as err:
            surveillance_station = {"error": str(err)}
    return async_redact_data(
        {
            "entry": {"unique_id": entry.unique_id, "data": dict(entry.data), "options": dict(entry.options)},
            "surveillance_station": surveillance_station,
            "playback": hass.data[DATA_MANAGER].stats(),
            "frigate": bridge.stats() if (bridge := hass.data.get(DATA_FRIGATE, {}).get(entry.entry_id)) else None,
        },
        TO_REDACT,
    )
