"""The real-time stream relay."""

import asyncio
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator
from synology_ss_playback import SSConnectionError

from custom_components.surveillance_station.views import DATA_MANAGER
from homeassistant.core import HomeAssistant


class FakeUpstream:
    """SS's stream socket: yields queued messages until closed."""

    def __init__(self, messages: list[bytes]) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        for m in messages:
            self.queue.put_nowait(m)
        self.sent: list[str] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        data = await self.queue.get()
        if data is None:
            raise StopAsyncIteration
        return aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, data, None)

    async def send_str(self, s: str) -> None:
        self.sent.append(s)

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


def _msg(header: str, payload: bytes = b"") -> bytes:
    h = header.encode()
    return (4 + len(h)).to_bytes(4, "big") + h + payload


async def _live_url(hass: HomeAssistant, hass_ws_client: WebSocketGenerator, camera_id: int = 10) -> str:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/live", "camera_id": camera_id})
    return (await ws.receive_json())["result"]["url"]


async def test_relay(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Messages pass through unchanged; nothing goes back; the token works once."""
    info = _msg("vdoCodec=H265&adoCodec=MPEG4-GENERIC")
    frame = _msg("mediaType=1&msec=1790000000000&key=1", b"moof+mdat")
    upstream = FakeUpstream([frame])
    mock_client.open_live = AsyncMock(return_value=(upstream, aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, info, None)))
    url = await _live_url(hass, hass_ws_client)
    client = await hass_client_no_auth()

    async with client.ws_connect(url) as ws:
        assert (await ws.receive_bytes()) == info
        assert (await ws.receive_bytes()) == frame
        await ws.send_str("keepAlive")
        await ws.send_str("time=2026-01-01T00:00:00")  # not passed on
        await asyncio.sleep(0.05)
        assert hass.data[DATA_MANAGER].stats()["live_streams"] == 1
        await upstream.close()  # SS ends the stream: so does the relay
        assert (await ws.receive()).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    mock_client.open_live.assert_awaited_once_with(10)
    assert upstream.sent == []  # HA keeps SS alive on its own timer
    await asyncio.sleep(0.05)
    assert hass.data[DATA_MANAGER].stats()["live_streams"] == 0
    assert (await client.get(url)).status == HTTPStatus.NOT_FOUND  # used up


async def test_bad_token_and_unreachable(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    client = await hass_client_no_auth()
    assert (await client.get("/api/surveillance_station/live/nope")).status == HTTPStatus.NOT_FOUND
    mock_client.open_live = AsyncMock(side_effect=SSConnectionError("ss_webstream_task", "connect", None))
    url = await _live_url(hass, hass_ws_client)
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await client.ws_connect(url)
    assert err.value.status == HTTPStatus.BAD_GATEWAY
    assert hass.data[DATA_MANAGER].stats()["live_streams"] == 0  # the slot is given back


async def test_unload_closes_streams(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    upstream = FakeUpstream([])
    mock_client.open_live = AsyncMock(
        return_value=(upstream, aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, _msg("vdoCodec=H265"), None))
    )
    url = await _live_url(hass, hass_ws_client)
    client = await hass_client_no_auth()
    async with client.ws_connect(url) as ws:
        await ws.receive_bytes()
        assert await hass.config_entries.async_unload(setup_integration.entry_id)
        assert (await ws.receive()).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    await asyncio.sleep(0.05)
    assert upstream.closed
    assert hass.data[DATA_MANAGER].stats()["live_streams"] == 0


async def test_plain_get_and_idle_browser(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A non-WebSocket GET is refused before SS is asked; a silent browser is dropped."""
    from custom_components.surveillance_station import views

    upstream = FakeUpstream([])
    mock_client.open_live = AsyncMock(
        return_value=(upstream, aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, _msg("vdoCodec=H265"), None))
    )
    client = await hass_client_no_auth()
    assert (await client.get(await _live_url(hass, hass_ws_client))).status == HTTPStatus.BAD_REQUEST
    mock_client.open_live.assert_not_awaited()

    views.LIVE_IDLE_SECONDS, saved = 0.2, views.LIVE_IDLE_SECONDS
    try:
        async with client.ws_connect(await _live_url(hass, hass_ws_client)) as ws:
            await ws.receive_bytes()
            assert (await ws.receive(timeout=2)).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    finally:
        views.LIVE_IDLE_SECONDS = saved
    await asyncio.sleep(0.05)
    assert upstream.closed
    assert hass.data[DATA_MANAGER].stats()["live_streams"] == 0
