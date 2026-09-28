"""SurveillanceStationClient.open_live: re-login on refusal, errors without the sid."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from synology_ss_playback import SSConnectionError, SSError, SurveillanceStationClient
from synology_ss_playback import client as client_mod

DATA = aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, b"\x00\x00\x00\x08info", None)
CLOSED = aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1000, None)


def _ws(first):
    ws = MagicMock()
    ws.receive = AsyncMock(return_value=first)
    ws.close = AsyncMock()
    return ws


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _answers(*codes):
    """What SS answers the session check (Info.GetInfo) with, in turn: an error code, or None for success."""
    it = iter(codes)

    def request(method, url, **kwargs):
        code = next(it)
        body = {"success": True, "data": {"serial": "S"}} if code is None else {"success": False, "error": {"code": code}}
        resp = MagicMock(status=200, headers={"Content-Type": "application/json"})
        resp.read = AsyncMock(return_value=json.dumps(body).encode())
        return _Ctx(resp)

    return MagicMock(side_effect=request)


def _client(sockets, checks=()):
    session = MagicMock()
    session.ws_connect = AsyncMock(side_effect=sockets)
    session.request = _answers(*checks)
    client = SurveillanceStationClient(session, "nas", 5000, False, "u", "p")
    sids = iter(["sid1", "sid2", "sid3"])

    async def login(stale_sid=None):
        client._sid = next(sids)

    client.login = AsyncMock(side_effect=login)
    return client, session


async def test_streams() -> None:
    ws = _ws(DATA)
    client, session = _client([ws])
    assert await client.open_live(10) == (ws, DATA)
    assert "ss_webstream_task/?camId=10&_sid=sid1" in session.ws_connect.await_args.args[0]


async def test_playback_from_a_time() -> None:
    """Epoch seconds, whole: SS reads a bare local time an hour off in DST."""
    client, session = _client([_ws(DATA)])
    await client.open_live(10, at=1790000000.7)
    assert session.ws_connect.await_args.args[0].endswith("camId=10&_sid=sid1&time=1790000000")


async def test_expired_sid_logs_in_again_once() -> None:
    refused, ok = _ws(CLOSED), _ws(DATA)
    client, session = _client([refused, ok], checks=[119, None])  # the check renews the session
    assert (await client.open_live(10))[0] is ok
    refused.close.assert_awaited()
    assert client.login.await_count == 2
    assert "_sid=sid2" in session.ws_connect.await_args.args[0]


async def test_refused_camera_never_replaces_a_valid_session() -> None:
    """An offline camera with a valid sid: no new DSM session (the old one would stay open)."""
    client, session = _client([_ws(CLOSED) for _ in range(2)], checks=[None])
    for _ in range(2):
        with pytest.raises(SSError, match="refused"):
            await client.open_live(99)
    # First try: the session checked, fine, given up. Second (within the minute): not even checked.
    assert client.login.await_count == 1
    assert session.request.call_count == 1
    assert session.ws_connect.await_count == 2


async def test_errors_never_carry_the_sid() -> None:
    client, session = _client([])
    client._sid = "secret-sid"
    session.ws_connect = AsyncMock(side_effect=aiohttp.ClientConnectionError("ws://nas/?_sid=secret-sid"))
    with pytest.raises(SSConnectionError) as err:
        await client.open_live(10)
    assert "secret-sid" not in str(err.value)


async def test_cancelled_while_waiting_closes_the_socket() -> None:
    ws = _ws(DATA)
    ws.receive = AsyncMock(side_effect=asyncio.CancelledError)
    client, _ = _client([ws])
    with pytest.raises(asyncio.CancelledError):
        await client.open_live(10)
    ws.close.assert_awaited()


async def test_no_data_within_the_timeout_closes_the_socket() -> None:
    """A connect that never sends anything (SS wedged) doesn't hang forever."""
    ws = _ws(DATA)  # never actually returned: the timeout wins first
    hang = asyncio.Event()

    async def never() -> aiohttp.WSMessage:
        await hang.wait()
        return DATA  # pragma: no cover - unreachable; hang is never set

    ws.receive = never
    client, _ = _client([ws])
    with patch.object(client_mod, "LIVE_CONNECT_TIMEOUT_SECONDS", 0.01):
        with pytest.raises(SSConnectionError, match="no data"):
            await client.open_live(10)
    ws.close.assert_awaited()
