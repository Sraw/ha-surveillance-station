"""Bookmark create / edit / delete: epoch times in, NAS-local times back."""

from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from synology_ss_playback import SSError, SurveillanceStationClient

API = "SYNO.SurveillanceStation.ThirdParty.Bookmark"
T = 1790000000  # 2026-09-21T07:13:20 in Los Angeles (summer time)


def _client(answer):
    client = SurveillanceStationClient(MagicMock(), "nas", 5000, False, "u", "p")
    client._tz = ZoneInfo("America/Los_Angeles")
    client._call = AsyncMock(return_value=answer)
    return client


def _stored(bid, name, comment, start, end):
    return {"bookmark": [{"bookmarkId": bid, "name": name, "comment": comment, "startTime": start, "endTime": end, "dsId": 0}]}


async def test_create() -> None:
    client = _client(_stored(12, 'a "b"', "c", "2026-09-21T07:13:20", "2026-09-21T07:13:50"))
    bm = await client.create_bookmark(6, 'a "b"', T + 0.9, T + 30, "c")
    assert (bm.id, bm.camera_id, bm.name, bm.comment, bm.start, bm.end) == (12, 6, 'a "b"', "c", T, T + 30)
    assert client._call.await_args.args == (API, "Create", 1)
    # Epoch seconds (a local time is read an hour off in summer); strings JSON-quoted.
    assert client._call.await_args.kwargs == {
        "camId": 6, "name": '"a \\"b\\""', "comment": '"c"', "startTime": T, "endTime": T + 30,
    }


async def test_end_never_before_start() -> None:
    client = _client(_stored(1, "x", "", "2026-09-21T07:13:20", ""))
    bm = await client.create_bookmark(6, "x", T, T - 5)
    assert client._call.await_args.kwargs["endTime"] == T
    assert bm.end == bm.start == T


async def test_edit() -> None:
    client = _client(_stored(12, "人", "", "2026-09-21T07:13:20", "2026-09-21T07:14:00"))
    bm = await client.edit_bookmark(12, 6, "人", T, T + 40)
    assert bm.end == T + 40
    assert client._call.await_args.args == (API, "Edit", 1)
    assert client._call.await_args.kwargs["bookmarkId"] == 12
    assert client._call.await_args.kwargs["name"] == '"人"'


async def test_unexpected_answer() -> None:
    with pytest.raises(SSError):
        await _client({}).create_bookmark(6, "x", T, T)


async def test_delete() -> None:
    client = _client({})
    await client.delete_bookmarks([3, 4])
    assert client._call.await_args.kwargs == {"bookmarkIds": "3,4"}
    client._call.reset_mock()
    await client.delete_bookmarks([])
    client._call.assert_not_awaited()
