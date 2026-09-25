"""WebSocket commands and the HLS views they hand out URLs for."""

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator
from synology_ss_playback import SSConnectionError

from homeassistant.core import HomeAssistant

from .conftest import T0


async def test_cameras(hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/cameras"})
    msg = await ws.receive_json()
    assert msg["success"]
    assert msg["result"] == {
        "entry_id": setup_integration.entry_id,
        "cameras": [
            {"id": 6, "name": "Drive Way", "enabled": True},
            {"id": 7, "name": "Backyard", "enabled": False},
        ],
    }


async def test_bookmarks_all_cameras(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, hass_ws_client: WebSocketGenerator
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/bookmarks", "start": T0, "end": T0 + 3600})
    msg = await ws.receive_json()
    assert msg["success"]
    assert msg["result"]["bookmarks"][0] == {
        "id": 1, "camera_id": 6, "name": "person", "comment": "", "start": T0 + 60, "end": T0 + 70,
    }
    # No camera_id: every camera.
    mock_client.bookmarks.assert_awaited_with(None, T0, T0 + 3600)


async def test_unreachable(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, hass_ws_client: WebSocketGenerator
) -> None:
    mock_client.cameras.side_effect = SSConnectionError("SYNO.SurveillanceStation.Camera", "List", None)
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/cameras"})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"] == {"code": "surveillance_station_error", "message": "Surveillance Station is unreachable"}


async def test_invalid_range(hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/recordings", "camera_id": 6, "start": T0, "end": T0})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"


async def test_vod_playlist_and_segments(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """An authenticated WS client gets a URL; the URL itself is the credential."""
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id(
        {"type": "surveillance_station/vod", "camera_id": 6, "start": T0 + 1790, "end": T0 + 1830}
    )
    msg = await ws.receive_json()
    assert msg["success"]
    res = msg["result"]
    assert not res["live"]
    # Two recordings back to back: one run, no gap.
    assert res["runs"] == [{"wall_start": T0 + 1790, "media_start": 0.0, "duration": 40.0}]

    client = await hass_client_no_auth()
    resp = await client.get(res["url"])
    assert resp.status == HTTPStatus.OK
    playlist = await resp.text()
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in playlist
    assert playlist.count("#EXTINF:") == 4  # 1790-1800, 1800-1810, ..., 1820-1830

    base = res["url"].rsplit("/", 1)[0]
    fetch = AsyncMock(return_value=(b"init", b"media"))
    with patch("custom_components.surveillance_station.views.fetch_segment", fetch):
        init = await client.get(f"{base}/init/0.mp4")
        seg = await client.get(f"{base}/seg/0.m4s")
        again = await client.get(f"{base}/seg/0.m4s")
    assert (await init.read(), await seg.read(), await again.read()) == (b"init", b"media", b"media")
    # Per-session URLs: nothing for the browser to keep.
    assert seg.headers["Cache-Control"] == "no-store"
    # One fetch from the NAS; init and repeats come from the cache.
    assert fetch.await_count == 1

    assert (await client.get(f"{base}/seg/99.m4s")).status == HTTPStatus.NOT_FOUND
    assert (await client.get("/api/surveillance_station/vod/not-a-token/index.m3u8")).status == HTTPStatus.NOT_FOUND


async def test_sessions_dropped_on_unload(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/vod", "camera_id": 6, "start": T0, "end": T0 + 60})
    url = (await ws.receive_json())["result"]["url"]
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    client = await hass_client_no_auth()
    assert (await client.get(url)).status == HTTPStatus.NOT_FOUND
