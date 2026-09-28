"""Bookmark create / edit / delete: epoch times in, NAS-local times back."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from synology_ss_playback import SSError, SurveillanceStationClient

API = "SYNO.SurveillanceStation.ThirdParty.Bookmark"
T = 1790000000  # 2026-09-21T07:13:20 in Los Angeles (summer time)


class _Ctx:
    def __init__(self, resp: MagicMock) -> None:
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _client(answer: dict[str, Any]) -> tuple[SurveillanceStationClient, list[dict[str, Any]]]:
    """A client of a NAS in Los Angeles that answers ``answer`` to every bookmark call,
    and the parameters of each request it got."""
    calls: list[dict[str, Any]] = []

    def request(method, url, **kwargs):
        params = kwargs.get("params") or kwargs["data"]
        calls.append(params)
        if params["api"] == "SYNO.API.Auth":
            data = {"sid": "s"}
        elif params["api"] == "SYNO.SurveillanceStation.Info":
            data = {"serial": "S", "timezoneTZDB": "America/Los_Angeles"}
        else:
            data = answer
        resp = MagicMock(status=200, headers={"Content-Type": "application/json"})
        resp.read = AsyncMock(return_value=json.dumps({"success": True, "data": data}).encode())
        return _Ctx(resp)

    session = MagicMock()
    session.request = MagicMock(side_effect=request)
    return SurveillanceStationClient(session, "nas", 5000, False, "u", "p"), calls


def _bookmark_call(calls: list[dict[str, Any]]) -> dict[str, Any]:
    [call] = [c for c in calls if c["api"] == API]
    return call


def _stored(bid, name, comment, start, end):
    return {"bookmark": [{"bookmarkId": bid, "name": name, "comment": comment, "startTime": start, "endTime": end, "dsId": 0}]}


async def test_create() -> None:
    client, calls = _client(_stored(12, 'a "b"', "c", "2026-09-21T07:13:20", "2026-09-21T07:13:50"))
    bm = await client.create_bookmark(6, 'a "b"', T + 0.9, T + 30, "c")
    assert (bm.id, bm.camera_id, bm.name, bm.comment, bm.start, bm.end) == (12, 6, 'a "b"', "c", T, T + 30)
    call = _bookmark_call(calls)
    assert (call["method"], call["version"]) == ("Create", 1)
    # Epoch seconds (a local time is read an hour off in summer); strings JSON-quoted.
    assert {k: call[k] for k in ("camId", "name", "comment", "startTime", "endTime")} == {
        "camId": 6, "name": '"a \\"b\\""', "comment": '"c"', "startTime": T, "endTime": T + 30,
    }


async def test_end_never_before_start() -> None:
    client, calls = _client(_stored(1, "x", "", "2026-09-21T07:13:20", ""))
    bm = await client.create_bookmark(6, "x", T, T - 5)
    assert _bookmark_call(calls)["endTime"] == T
    assert bm.end == bm.start == T


async def test_edit() -> None:
    client, calls = _client(_stored(12, "人", "", "2026-09-21T07:13:20", "2026-09-21T07:14:00"))
    bm = await client.edit_bookmark(12, 6, "人", T, T + 40)
    assert bm.end == T + 40
    call = _bookmark_call(calls)
    assert (call["method"], call["version"]) == ("Edit", 1)
    assert call["bookmarkId"] == 12
    assert call["name"] == '"人"'


async def test_unexpected_answer() -> None:
    client, _ = _client({})
    with pytest.raises(SSError):
        await client.create_bookmark(6, "x", T, T)


async def test_delete() -> None:
    client, calls = _client({})
    await client.delete_bookmarks([3, 4])
    call = _bookmark_call(calls)
    assert (call["method"], call["bookmarkIds"]) == ("Delete", "3,4")
    calls.clear()
    await client.delete_bookmarks([])
    assert calls == []
