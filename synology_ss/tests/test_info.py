"""SS Info: an incomplete answer is an SSError (retryable), not a KeyError."""

import threading
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from synology_ss_playback import SSError, SurveillanceStationClient
from synology_ss_playback import client as client_mod


def _client(answer):
    client = SurveillanceStationClient(MagicMock(), "nas", 5000, False, "u", "p")
    client._call = AsyncMock(return_value=answer)
    return client


async def test_info() -> None:
    info = await _client(
        {"serial": 1234, "hostname": "nas", "version": {"major": 9, "minor": 3, "small": 0, "build": "12143"},
         "timezoneTZDB": "America/Los_Angeles"}
    ).info()
    assert (info.serial, info.hostname, info.version, info.timezone) == ("1234", "nas", "9.3.0-12143", "America/Los_Angeles")


async def test_info_without_serial() -> None:
    with pytest.raises(SSError, match="without a serial"):
        await _client({"hostname": "nas"}).info()


async def test_info_with_empty_serial() -> None:
    with pytest.raises(SSError, match="without a serial"):
        await _client({"serial": ""}).info()


async def test_missing_time_zone_is_not_cached() -> None:
    """UTC for now, but asked again next time: it may have been a partial answer."""
    client = _client({"serial": 1})
    assert await client._timezone() == ZoneInfo("UTC")
    client._call.return_value = {"serial": 1, "timezoneTZDB": "America/Los_Angeles"}
    assert await client._timezone() == ZoneInfo("America/Los_Angeles")
    assert await client._timezone() == ZoneInfo("America/Los_Angeles")
    assert client._call.await_count == 2


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "Not/AZone", None])
async def test_time_zone_is_loaded_off_the_event_loop(monkeypatch: pytest.MonkeyPatch, zone: str | None) -> None:
    """A zone's first load reads tzdata from disk: HA flags that on its loop."""
    loaded_on = []

    def load(key: str) -> ZoneInfo:
        loaded_on.append(threading.get_ident())
        return ZoneInfo(key)

    monkeypatch.setattr(client_mod, "ZoneInfo", load)
    client = _client({"serial": 1, "timezoneTZDB": zone})
    await client.info()
    await client.timezone()
    assert loaded_on and threading.get_ident() not in loaded_on
