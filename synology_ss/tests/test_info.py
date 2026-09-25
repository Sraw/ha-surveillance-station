"""SS Info: an incomplete answer is an SSError (retryable), not a KeyError."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from synology_ss_playback import SSError, SurveillanceStationClient


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
