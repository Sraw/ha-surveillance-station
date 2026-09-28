"""The real-time stream relay."""

import asyncio
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator
from synology_ss_playback import SSConnectionError

from custom_components.surveillance_station.manager import DATA_MANAGER
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
        if isinstance(data, aiohttp.WSMessage):
            return data  # a caller queued a specific message type (e.g. CLOSE)
        return aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, data, None)

    async def send_str(self, s: str) -> None:
        self.sent.append(s)

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


def _msg(header: str, payload: bytes = b"") -> bytes:
    h = header.encode()
    return (4 + len(h)).to_bytes(4, "big") + h + payload


async def _live_url(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, camera_id: int = 10, **extra
) -> str:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/live", "camera_id": camera_id, **extra})
    return (await ws.receive_json())["result"]["url"]


async def test_relay(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Messages pass through unchanged; the token works once."""
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
    mock_client.open_live.assert_awaited_once_with(10, at=None)
    assert upstream.sent == []  # HA keeps SS alive on its own timer
    await asyncio.sleep(0.05)
    assert hass.data[DATA_MANAGER].stats()["live_streams"] == 0
    assert (await client.get(url)).status == HTTPStatus.NOT_FOUND  # used up


async def test_relay_is_not_compressed(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Browsers offer permessage-deflate; video doesn't deflate, so it is declined."""
    upstream = FakeUpstream([])
    mock_client.open_live = AsyncMock(
        return_value=(upstream, aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, _msg("vdoCodec=H265"), None))
    )
    url = await _live_url(hass, hass_ws_client)
    client = await hass_client_no_auth()
    async with client.ws_connect(url, compress=15) as ws:
        await ws.receive_bytes()
        assert ws.compress == 0
        await upstream.close()


async def test_entry_unloaded_between_token_and_connect(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The token is still valid, but the entry no longer is (client() is None)."""
    url = await _live_url(hass, hass_ws_client)
    client = await hass_client_no_auth()
    manager = hass.data[DATA_MANAGER]
    with patch.object(manager, "client", return_value=None):
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await client.ws_connect(url)
    assert err.value.status == HTTPStatus.NOT_FOUND


async def test_entry_reloaded_while_connecting(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    mock_client: MagicMock,
) -> None:
    """Unloaded while SS was connecting: that stream isn't relayed on a logged-out client."""
    upstream = FakeUpstream([b"x"])
    manager = hass.data[DATA_MANAGER]
    mock_client.open_live = AsyncMock(return_value=(upstream, aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, b"info", None)))
    url = await _live_url(hass, hass_ws_client)
    client = await hass_client_no_auth()
    # Asked before connecting: this client; after: the reloaded entry's new one.
    with patch.object(manager, "client", side_effect=[mock_client, MagicMock()]), pytest.raises(
        aiohttp.WSServerHandshakeError
    ) as err:
        await client.ws_connect(url)
    assert err.value.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert upstream.closed
    assert manager.stats()["live_streams"] == 0


async def test_unload_leaves_a_connecting_stream_alone(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Not prepared yet (still connecting to SS): close() would raise; it notices the unload itself."""
    manager = hass.data[DATA_MANAGER]
    connecting = MagicMock(prepared=False)
    connecting.close = AsyncMock()
    manager.live_streams.add((setup_integration.entry_id, connecting))
    manager.drop_entry(setup_integration.entry_id)
    await hass.async_block_till_done()
    connecting.close.assert_not_called()
    manager.live_streams.clear()


async def test_live_stream_cap(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    from custom_components.surveillance_station import views

    url = await _live_url(hass, hass_ws_client)
    client = await hass_client_no_auth()
    with patch.object(views, "MAX_LIVE_STREAMS", 0):
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await client.ws_connect(url)
    assert err.value.status == HTTPStatus.SERVICE_UNAVAILABLE
    mock_client.open_live.assert_not_called()


async def test_relay_ends_when_upstream_sends_a_non_binary_message(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """SS closing its end mid-stream (rather than the socket) also ends the relay."""
    upstream = FakeUpstream([])
    upstream.queue.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1000, None))
    mock_client.open_live = AsyncMock(
        return_value=(upstream, aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, _msg("vdoCodec=H265"), None))
    )
    url = await _live_url(hass, hass_ws_client)
    client = await hass_client_no_auth()
    async with client.ws_connect(url) as ws:
        await ws.receive_bytes()
        assert (await ws.receive()).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    await asyncio.sleep(0.05)
    assert hass.data[DATA_MANAGER].stats()["live_streams"] == 0


async def test_keep_alive_pings_surveillance_station(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    from custom_components.surveillance_station import views

    upstream = FakeUpstream([])
    mock_client.open_live = AsyncMock(
        return_value=(upstream, aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, _msg("vdoCodec=H265"), None))
    )
    url = await _live_url(hass, hass_ws_client)
    client = await hass_client_no_auth()
    with patch.object(views, "LIVE_KEEP_ALIVE_SECONDS", 0.01):  # the real 10 s wait, sped up (not 0: no busy spin)
        async with client.ws_connect(url) as ws:
            await ws.receive_bytes()
            await asyncio.sleep(0.05)
            await upstream.close()
    assert "keepAlive" in upstream.sent


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


async def test_playback_commands(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A playback stream starts at the time asked; only the steering commands reach SS."""
    upstream = FakeUpstream([])
    mock_client.open_live = AsyncMock(
        return_value=(upstream, aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, _msg("vdoCodec=H265"), None))
    )
    url = await _live_url(hass, hass_ws_client, time=1790000000.5)
    client = await hass_client_no_auth()
    passed = ["time=1790000100", "pause=true", "pause=false", "speed=0.5", "speed=8", "speed=16"]
    refused = [
        "keepAlive",
        "time=2026-01-01T00:00:00",
        "time=1790000100&camId=3",
        "time=1790000100\n",
        "speed=3",
        "pause=1",
        "_sid=x",
        "time=\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u0660",  # Arabic-Indic digits
        "time=\uff11\uff17\uff19\uff10\uff10\uff10\uff10\uff10\uff10\uff10",  # full-width
    ]
    async with client.ws_connect(url) as ws:
        await ws.receive_bytes()
        for s in refused + passed:
            await ws.send_str(s)
        await ws.send_bytes(b"time=1790000100")
        await asyncio.sleep(0.05)
        await upstream.close()
    mock_client.open_live.assert_awaited_once_with(10, at=1790000000.5)
    assert upstream.sent == passed


async def test_bad_playback_time(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    ws = await hass_ws_client(hass)
    for bad in (-5, 1e300, "inf", "nan"):
        await ws.send_json_auto_id({"type": "surveillance_station/live", "camera_id": 10, "time": bad})
        assert not (await ws.receive_json())["success"], bad
