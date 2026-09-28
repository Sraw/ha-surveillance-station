"""SurveillanceStationClient: login, session-error retries, and the API calls."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from synology_ss_playback import (
    Bookmark,
    Camera,
    RecordingInfo,
    SSAuthError,
    SSConnectionError,
    SSError,
    SSInfo,
    SurveillanceStationClient,
)


def _resp(status=200, body=b"{}", content_type="application/json"):
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.headers = {"Content-Type": content_type}
    return resp


class _Ctx:
    """What ``session.request(...)`` returns: an async context manager."""

    def __init__(self, resp: MagicMock) -> None:
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _client(responses: list) -> tuple[SurveillanceStationClient, MagicMock]:
    """A client whose session.request() returns each of ``responses`` in turn
    (a MagicMock response, or an Exception instance to raise)."""
    it = iter(responses)

    def request(method, url, **kwargs):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return _Ctx(nxt)

    session = MagicMock()
    session.request = MagicMock(side_effect=request)
    client = SurveillanceStationClient(session, "nas", 5000, False, "u", "p")
    return client, session


def _json(data: dict) -> bytes:
    import json

    return json.dumps(data).encode()


async def test_login_success() -> None:
    client, session = _client([_resp(body=_json({"success": True, "data": {"sid": "abc"}}))])
    await client.login()
    assert client._sid == "abc"
    # POST, so the password never sits in a URL.
    assert session.request.call_args.args[0] == "POST"


async def test_login_auth_failed_is_sticky() -> None:
    """A rejected password isn't retried (that would trip DSM's auto-block)."""
    called = MagicMock()
    client, session = _client([_resp(body=_json({"success": False, "error": {"code": 400}}))])
    client.on_auth_failed = called
    with pytest.raises(SSAuthError):
        await client.login()
    assert client._sid is None
    called.assert_called_once()
    # A second attempt fails immediately, without asking Surveillance Station again.
    with pytest.raises(SSAuthError):
        await client.login()
    assert session.request.call_count == 1


async def test_login_other_error_is_not_sticky() -> None:
    client, session = _client(
        [
            _resp(body=_json({"success": False, "error": {"code": 119}})),
            _resp(body=_json({"success": True, "data": {"sid": "abc"}})),
        ]
    )
    with pytest.raises(SSError) as err:
        await client.login()
    assert not isinstance(err.value, SSAuthError)
    await client.login()
    assert client._sid == "abc"


async def test_login_skipped_if_already_logged_in() -> None:
    client, session = _client([_resp(body=_json({"success": True, "data": {"sid": "abc"}}))])
    await client.login()
    await client.login()  # no new request: the sid is still current
    assert session.request.call_count == 1


async def test_login_retries_if_sid_is_stale() -> None:
    """Concurrent callers pass the sid they saw; a caller behind a fresh one skips."""
    client, session = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "old"}})),
            _resp(body=_json({"success": True, "data": {"sid": "new"}})),
            _resp(body=_json({"success": True})),
        ]
    )
    await client.login()
    await client.login(stale_sid="old")
    await client.login(stale_sid="old")  # replaced already: no login, no logout
    assert client._sid == "new"
    assert session.request.call_count == 3


async def test_relogin_logs_out_the_replaced_session() -> None:
    """The old session may still be valid (a camera SS won't stream, a 105): DSM
    would keep it open until it times out."""
    client, session = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "old"}})),
            _resp(body=_json({"success": True, "data": {"sid": "new"}})),
            _resp(status=500),  # its logout fails: swallowed
        ]
    )
    await client.login()
    await client.login(stale_sid="old")
    params = session.request.call_args.kwargs["params"]
    assert (params["method"], params["_sid"]) == ("logout", "old")
    assert client._sid == "new"


async def test_logout_noop_without_a_session() -> None:
    client, session = _client([])
    await client.logout()
    assert session.request.call_count == 0


async def test_logout_clears_sid_even_on_error() -> None:
    client, session = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "abc"}})),
            _resp(status=500),
        ]
    )
    await client.login()
    await client.logout()  # the failure is swallowed
    assert client._sid is None


async def test_info_builds_version_and_timezone() -> None:
    data = {
        "success": True,
        "data": {
            "serial": "SN1",
            "hostname": "nas1",
            "version": {"major": 9, "minor": 3, "small": 0, "build": 12143},
            "timezoneTZDB": "US/Pacific",
        },
    }
    client, _ = _client([_resp(body=_json({"success": True, "data": {"sid": "s"}})), _resp(body=_json(data))])
    info = await client.info()
    assert info == SSInfo(serial="SN1", hostname="nas1", version="9.3.0-12143", timezone="US/Pacific")


async def test_info_missing_timezone_warns_and_assumes_utc(caplog: pytest.LogCaptureFixture) -> None:
    data = {"success": True, "data": {"serial": "SN1", "version": {"major": 9, "minor": 0, "small": 0}}}
    client, _ = _client([_resp(body=_json({"success": True, "data": {"sid": "s"}})), _resp(body=_json(data))])
    info = await client.info()
    assert info.timezone == "UTC"
    assert "no time zone" in caplog.text


async def test_info_unknown_timezone_falls_back_to_utc(caplog: pytest.LogCaptureFixture) -> None:
    data = {
        "success": True,
        "data": {"serial": "SN1", "version": {"major": 9, "minor": 0, "small": 0}, "timezoneTZDB": "Not/AZone"},
    }
    client, _ = _client([_resp(body=_json({"success": True, "data": {"sid": "s"}})), _resp(body=_json(data))])
    await client.info()
    assert client._tz.key == "UTC"
    assert "Unknown NAS time zone" in caplog.text


async def test_timezone_calls_info_once_if_unset() -> None:
    data = {"success": True, "data": {"serial": "SN1", "version": {"major": 9, "minor": 0, "small": 0}, "timezoneTZDB": "UTC"}}
    client, session = _client([_resp(body=_json({"success": True, "data": {"sid": "s"}})), _resp(body=_json(data))])
    tz = await client._timezone()
    assert tz.key == "UTC"
    assert session.request.call_count == 2  # login + info, not info again


async def test_cameras() -> None:
    data = {
        "success": True,
        "data": {"cameras": [{"id": 6, "newName": "Drive Way", "enabled": True}, {"id": 7, "name": "Backyard"}]},
    }
    client, _ = _client([_resp(body=_json({"success": True, "data": {"sid": "s"}})), _resp(body=_json(data))])
    cams = await client.cameras()
    assert cams == [Camera(id=6, name="Drive Way", enabled=True), Camera(id=7, name="Backyard", enabled=True)]


async def test_recordings_filters_and_paginates() -> None:
    page1 = {
        "success": True,
        "data": {
            "total": 3,
            "events": [
                {"id": 1, "cameraId": 6, "startTime": 100, "stopTime": 199, "mountId": 1, "videoCodec": 6},
                {"id": 2, "cameraId": 6, "startTime": 200, "stopTime": 90, "deleted": True},  # deleted: skipped
            ],
        },
    }
    page2 = {
        "success": True,
        "data": {
            "total": 3,
            "events": [
                # stopTime before the window and not recording: skipped.
                {"id": 3, "cameraId": 6, "startTime": 0, "stopTime": 5, "recording": False},
            ],
        },
    }
    client, session = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "s"}})),
            _resp(body=_json(page1)),
            _resp(body=_json(page2)),
        ]
    )
    recs = await client.recordings(6, 50, 300)
    assert recs == [RecordingInfo(id=1, camera_id=6, start=100, end=199, mount_id=1, live=False, hevc=True)]
    assert session.request.call_count == 3  # login + 2 pages


async def test_recordings_stops_when_a_page_is_empty() -> None:
    empty = {"success": True, "data": {"total": 5, "events": []}}
    client, _ = _client([_resp(body=_json({"success": True, "data": {"sid": "s"}})), _resp(body=_json(empty))])
    assert await client.recordings(6, 0, 10) == []


async def test_list_bookmarks_empty_ids_skips_the_call() -> None:
    client, session = _client([])
    assert await client.list_bookmarks([]) == []
    assert session.request.call_count == 0


async def test_list_bookmarks_sorted_newest_first() -> None:
    tzinfo = {"success": True, "data": {"serial": "SN1", "version": {"major": 9, "minor": 0, "small": 0}, "timezoneTZDB": "UTC"}}
    data = {
        "success": True,
        "data": {
            "bookmarks": [
                {"bookmarkId": 1, "camId": 6, "name": "a", "startTime": "2026-01-01T00:00:00"},
                {"bookmarkId": 2, "camId": 6, "name": "b", "comment": "c", "startTime": "2026-01-01T00:01:00", "endTime": "2026-01-01T00:01:05"},
            ]
        },
    }
    client, _ = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "s"}})),
            _resp(body=_json(tzinfo)),
            _resp(body=_json(data)),
        ]
    )
    out = await client.list_bookmarks([6])
    assert [b.id for b in out] == [2, 1]
    assert out[0] == Bookmark(id=2, camera_id=6, name="b", comment="c", start=1767225660, end=1767225665)


async def test_list_bookmarks_decoded_and_parsed_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The list holds every bookmark: its JSON, times and sort run in a thread; other answers don't."""
    import synology_ss_playback.client as client_mod

    loop = threading.get_ident()
    decoded: list[tuple[bool, int]] = []
    parsed: list[int] = []

    class _Json:
        def __getattr__(self, name):
            return getattr(json, name)

        def loads(self, body):
            data = json.loads(body)
            decoded.append(("bookmarks" in data.get("data", {}), threading.get_ident()))
            return data

    local_ts = client_mod._local_ts

    def spied_local_ts(value, tz):
        parsed.append(threading.get_ident())
        return local_ts(value, tz)

    monkeypatch.setattr(client_mod, "json", _Json())
    monkeypatch.setattr(client_mod, "_local_ts", spied_local_ts)
    tzinfo = {"success": True, "data": {"serial": "SN1", "timezoneTZDB": "UTC"}}
    data = {"success": True, "data": {"bookmarks": [
        {"bookmarkId": i, "camId": 6, "name": "a", "startTime": f"2026-01-01T00:0{i}:00"} for i in (1, 3, 2)
    ]}}
    client, _ = _client([_resp(body=_json(_LOGIN)), _resp(body=_json(tzinfo)), _resp(body=_json(data))])
    assert [b.id for b in await client.list_bookmarks([6])] == [3, 2, 1]
    assert [(listed, ident == loop) for listed, ident in decoded] == [(False, True), (False, True), (True, False)]
    assert parsed and loop not in parsed


async def test_download_success() -> None:
    client, _ = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "s"}})),
            _resp(body=b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4"),
        ]
    )
    assert await client.download(1, 1, 0, 1000) == b"\x00\x00\x00\x18ftypmp42"


async def test_download_retries_once_on_session_error_then_raises() -> None:
    client, session = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "s1"}})),
            _resp(body=_json({"error": {"code": 106}})),  # session expired
            _resp(body=_json({"success": True, "data": {"sid": "s2"}})),  # re-login
            _resp(body=_json({"success": True})),  # s1 logged out
            _resp(body=_json({"error": {"code": 400}})),  # still fails
        ]
    )
    with pytest.raises(SSError) as err:
        await client.download(1, 1, 0, 1000)
    assert not isinstance(err.value, SSAuthError)
    assert session.request.call_count == 5


async def test_call_retries_once_on_session_error() -> None:
    client, session = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "s1"}})),
            _resp(body=_json({"success": False, "error": {"code": 105}})),
            _resp(body=_json({"success": True, "data": {"sid": "s2"}})),
            _resp(body=_json({"success": True})),  # s1 logged out
            _resp(body=_json({"success": True, "data": {"cameras": []}})),
        ]
    )
    assert await client.cameras() == []
    assert session.request.call_count == 5


async def test_no_permission_after_a_new_login_is_not_retried_again() -> None:
    """105 is retried with a new session once; refused again, it is the account's
    permission, and that call no longer logs in (a Frigate review each time)."""
    ok = _json({"success": True})
    no = _json({"success": False, "error": {"code": 105}})

    def sid(s: str) -> MagicMock:
        return _resp(body=_json({"success": True, "data": {"sid": s}}))

    client, session = _client(
        [
            sid("s1"), _resp(body=no), sid("s2"), _resp(body=ok), _resp(body=no),  # retried once
            _resp(body=no),  # the same call: not retried
            _resp(body=no), sid("s3"), _resp(body=ok), _resp(body=ok),  # another call still is
            _resp(body=no), sid("s4"), _resp(body=ok), _resp(body=_json({"success": True, "data": {"cameras": []}})),
        ]
    )
    for _ in range(2):
        with pytest.raises(SSError) as err:
            await client.cameras()
        assert err.value.code == 105
    assert session.request.call_count == 6
    await client.delete_bookmarks([1])
    assert session.request.call_count == 10
    # A new session since: retried with a login again.
    assert await client.cameras() == []
    assert session.request.call_count == 14


async def test_request_http_error() -> None:
    client, _ = _client([_resp(body=_json({"success": True, "data": {"sid": "s"}})), _resp(status=500)])
    with pytest.raises(SSError):
        await client.cameras()


async def test_request_connection_error_never_carries_the_sid() -> None:
    client, _ = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "secret-sid"}})),
            aiohttp.ClientConnectionError("boom"),
        ]
    )
    with pytest.raises(SSConnectionError) as err:
        await client.cameras()
    assert "secret-sid" not in str(err.value)


async def test_raw_json_non_json_reply() -> None:
    client, _ = _client([_resp(body=b"not json")])
    with pytest.raises(SSError, match="non-JSON"):
        await client._raw_json("auth.cgi", {"api": "x", "method": "y"})


async def test_recordings_reads_videocodec_as_a_name() -> None:
    """Some calls report the codec by name rather than the numeric id."""
    data = {
        "success": True,
        "data": {
            "total": 1,
            "events": [{"id": 1, "cameraId": 6, "startTime": 0, "stopTime": 10, "videoCodec": "H265"}],
        },
    }
    client, _ = _client([_resp(body=_json({"success": True, "data": {"sid": "s"}})), _resp(body=_json(data))])
    recs = await client.recordings(6, 0, 10)
    assert recs[0].hevc is True


async def test_call_raises_immediately_on_a_non_session_error() -> None:
    client, session = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "s"}})),
            _resp(body=_json({"success": False, "error": {"code": 999}})),
        ]
    )
    with pytest.raises(SSError):
        await client.cameras()
    assert session.request.call_count == 2  # not retried: 999 isn't a session error


async def test_download_malformed_json_error_body() -> None:
    """A body that looks like JSON (starts with '{') but doesn't parse."""
    client, _ = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "s"}})),
            _resp(body=b"{not valid json", content_type="application/json"),
        ]
    )
    with pytest.raises(SSError):
        await client.download(1, 1, 0, 1000)


# --- malformed answers are SSErrors (retryable), never AttributeError/KeyError ---

_LOGIN = {"success": True, "data": {"sid": "s"}}


@pytest.mark.parametrize("body", [b"[]", b"null", b'"x"', b"42"])
async def test_non_object_reply_is_an_sserror(body: bytes) -> None:
    client, _ = _client([_resp(body=body)])
    with pytest.raises(SSError, match="unexpected reply"):
        await client.login()


@pytest.mark.parametrize(
    "answer",
    [{"success": True}, {"success": True, "data": []}, {"success": True, "data": {"sid": ""}}, {"success": True, "data": {"sid": 5}}],
)
async def test_login_without_a_session_is_an_sserror(answer: dict) -> None:
    client, _ = _client([_resp(body=_json(answer))])
    with pytest.raises(SSError, match="without a session"):
        await client.login()


async def test_call_with_odd_data_or_error() -> None:
    client, _ = _client(
        [_resp(body=_json(_LOGIN)), _resp(body=_json({"success": True, "data": [1]})), _resp(body=_json({"success": False, "error": "x"}))]
    )
    with pytest.raises(SSError, match="unexpected reply"):
        await client.cameras()
    with pytest.raises(SSError) as err:
        await client.cameras()
    assert err.value.code is None


async def test_malformed_timezone_key_falls_back_to_utc() -> None:
    client, _ = _client([_resp(body=_json(_LOGIN)), _resp(body=_json({"success": True, "data": {"serial": "S", "timezoneTZDB": "../etc"}}))])
    await client.info()
    assert str(await client._timezone()) == "UTC"


async def test_odd_list_entries_are_skipped() -> None:
    """One entry SS lists oddly must not hide all the others."""
    tz = {"success": True, "data": {"serial": "S", "timezoneTZDB": "UTC"}}
    bookmarks = {"success": True, "data": {"bookmarks": [
        {"bookmarkId": 1, "camId": 6, "startTime": "2026-01-01T00:00:00"},
        {"bookmarkId": 2, "camId": 6, "startTime": "not a time"},
        {"camId": 6, "startTime": "2026-01-01T00:00:00"},
        "junk",
    ]}}
    cameras = {"success": True, "data": {"cameras": [{"id": 6, "name": "A"}, {"name": "no id"}, None]}}
    events = {"success": True, "data": {"total": "x", "events": [
        {"id": 1, "cameraId": 6, "startTime": 10, "stopTime": 20},
        {"id": 2, "cameraId": 6, "startTime": "?", "stopTime": 20},
    ]}}
    client, _ = _client([_resp(body=_json(_LOGIN)), _resp(body=_json(tz)), _resp(body=_json(bookmarks)),
                         _resp(body=_json(cameras)), _resp(body=_json(events))])
    assert [b.id for b in await client.list_bookmarks([6])] == [1]
    assert [c.id for c in await client.cameras()] == [6]
    assert [r.id for r in await client.recordings(6, 0, 30)] == [1]


async def test_bad_create_answer_is_an_sserror() -> None:
    client, _ = _client([_resp(body=_json(_LOGIN)), _resp(body=_json({"success": True, "data": {"serial": "S", "timezoneTZDB": "UTC"}})),
                         _resp(body=_json({"success": True, "data": {"bookmark": [{"bookmarkId": 1, "startTime": "bad"}]}}))])
    await client.info()
    with pytest.raises(SSError):
        await client.create_bookmark(6, "x", 0, 1)


async def test_download_error_body_not_an_object() -> None:
    client, _ = _client([_resp(body=_json(_LOGIN)), _resp(body=b"[1]", content_type="application/json")])
    with pytest.raises(SSError) as err:
        await client.download(1, 1, 0, 1000)
    assert err.value.code is None


# --- refused logins ---


async def test_blocked_ip_is_not_a_bad_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """407: DSM blocked this host for a while; the password is fine, so no reauth.
    No login is tried for a minute (each could extend the block)."""
    import synology_ss_playback.client as client_mod

    now = [1000.0]
    monkeypatch.setattr(client_mod, "_monotonic", lambda: now[0])
    called = MagicMock()
    client, session = _client([_resp(body=_json({"success": False, "error": {"code": 407}})), _resp(body=_json(_LOGIN))])
    client.on_auth_failed = called
    with pytest.raises(SSError) as err:
        await client.login()
    assert not isinstance(err.value, SSAuthError)
    called.assert_not_called()
    with pytest.raises(SSError, match="blocked"):
        await client.login()
    assert session.request.call_count == 1
    now[0] += client_mod.BLOCKED_SECONDS
    await client.login()
    assert client._sid == "s"


async def test_refused_login_tried_again_after_a_while(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not every refusal is about the password (a directory service not up yet after a DSM reboot)."""
    import synology_ss_playback.client as client_mod

    now = [1000.0]
    monkeypatch.setattr(client_mod, "_monotonic", lambda: now[0])
    client, session = _client([_resp(body=_json({"success": False, "error": {"code": 400}})), _resp(body=_json(_LOGIN))])
    with pytest.raises(SSAuthError):
        await client.login()
    now[0] += client_mod.AUTH_RETRY_SECONDS - 1
    with pytest.raises(SSAuthError):
        await client.login()
    assert session.request.call_count == 1
    now[0] += 2
    await client.login()
    assert client._sid == "s" and client._auth_failed is None


async def test_logout_is_quick() -> None:
    """An unload waits for it: a NAS that is gone mustn't hold it for 30 s."""
    client, session = _client([_resp(body=_json(_LOGIN)), _resp(body=_json({"success": True}))])
    await client.login()
    await client.logout()
    assert session.request.call_args.kwargs["timeout"].total == 5


async def test_missing_apis() -> None:
    """What the NAS lacks of the Web APIs used, or has only older versions of (SYNO.API.Info, no login)."""
    from synology_ss_playback.client import REQUIRED_APIS

    apis = {api: {"minVersion": 1, "maxVersion": v} for api, v in REQUIRED_APIS.items()}
    apis["SYNO.SurveillanceStation.Event"] = {"minVersion": 1, "maxVersion": 3}  # SS too old
    del apis["SYNO.SurveillanceStation.ThirdParty.Bookmark"]
    apis["SYNO.SurveillanceStation.Camera"] = {"minVersion": "x"}  # odd
    client, session = _client([_resp(body=_json({"success": True, "data": apis}))])
    assert await client.missing_apis() == [
        "SYNO.SurveillanceStation.Camera v9",
        "SYNO.SurveillanceStation.Event v5",
        "SYNO.SurveillanceStation.ThirdParty.Bookmark v1",
    ]
    assert session.request.call_args.args[1].endswith("/webapi/query.cgi")
    client, _ = _client([_resp(body=_json({"success": False, "error": {"code": 102}}))])
    with pytest.raises(SSError):
        await client.missing_apis()
    # The SS package stopped (or updating): none of its APIs there. Not "old".
    client, _ = _client([_resp(body=_json({"success": True, "data": {"SYNO.API.Auth": {"minVersion": 1, "maxVersion": 7}}}))])
    with pytest.raises(SSConnectionError, match="isn't running"):
        await client.missing_apis()


def test_ipv6_host() -> None:
    client = SurveillanceStationClient(MagicMock(), "fd00::5", 5001, True, "u", "p")
    assert client._base == "https://[fd00::5]:5001/webapi"


# --- closed (the entry unloaded) ---


async def test_close_logs_out_and_never_logs_in_again() -> None:
    """A handler still running after an unload must not open a new DSM session."""
    client, session = _client([_resp(body=_json(_LOGIN)), _resp(body=_json({"success": True}))])
    await client.login()
    await client.close()
    assert session.request.call_args.kwargs["params"]["method"] == "logout"
    with pytest.raises(SSError, match="closed"):
        await client.cameras()
    assert session.request.call_count == 2


async def test_a_login_on_its_way_when_closed_is_logged_out() -> None:
    answered = asyncio.Event()
    login = _resp()

    async def read() -> bytes:
        await answered.wait()
        return _json({"success": True, "data": {"sid": "late"}})

    login.read = AsyncMock(side_effect=read)
    client, session = _client([login, _resp(body=_json({"success": True}))])
    call = asyncio.create_task(client.cameras())
    while not session.request.called:
        await asyncio.sleep(0)
    await client.close()  # no session yet: nothing to log out
    answered.set()
    with pytest.raises(SSError, match="closed"):
        await call
    params = session.request.call_args.kwargs["params"]
    assert (params["method"], params["_sid"]) == ("logout", "late")
    assert client._sid is None


# --- a camera's recordings, shared by identical lookups ---


def _events(*ids: int) -> MagicMock:
    events = [{"id": i, "cameraId": 6, "startTime": 0, "stopTime": 10} for i in ids]
    return _resp(body=_json({"success": True, "data": {"total": len(events), "events": events}}))


async def test_recordings_are_shared_for_a_moment(monkeypatch: pytest.MonkeyPatch) -> None:
    import synology_ss_playback.client as client_mod

    now = [1000.0]
    monkeypatch.setattr(client_mod, "_monotonic", lambda: now[0])
    client, session = _client([_resp(body=_json(_LOGIN)), _events(1), _events(2), _events(3)])
    first = await client.recordings(6, 0, 10)
    first.clear()  # a caller's list is its own
    assert [r.id for r in await client.recordings(6, 0, 10)] == [1]
    assert session.request.call_count == 2
    assert [r.id for r in await client.recordings(6, 0, 11)] == [2]  # another window
    now[0] += client_mod.RECORDINGS_CACHE_SECONDS
    assert [r.id for r in await client.recordings(6, 0, 10)] == [3]  # asked again
    assert session.request.call_count == 4


async def test_concurrent_lookups_share_one_request() -> None:
    client, session = _client([_resp(body=_json(_LOGIN)), _events(1)])
    a, b = await asyncio.gather(client.recordings(6, 0, 10), client.recordings(6, 0, 10))
    assert [r.id for r in a] == [r.id for r in b] == [1]
    assert session.request.call_count == 2


async def test_a_caller_going_away_leaves_the_lookup_to_the_others() -> None:
    answered = asyncio.Event()
    listed = _events(1)
    body = await listed.read()

    async def read() -> bytes:
        await answered.wait()
        return body

    listed.read = AsyncMock(side_effect=read)
    client, session = _client([_resp(body=_json(_LOGIN)), listed])
    gone = asyncio.create_task(client.recordings(6, 0, 10))
    stays = asyncio.create_task(client.recordings(6, 0, 10))
    while session.request.call_count < 2:
        await asyncio.sleep(0)
    gone.cancel()
    answered.set()
    assert [r.id for r in await stays] == [1]
    with pytest.raises(asyncio.CancelledError):
        await gone
    assert session.request.call_count == 2


async def test_a_failed_lookup_is_not_kept() -> None:
    client, session = _client([_resp(body=_json(_LOGIN)), _resp(status=500), _events(1)])
    with pytest.raises(SSError):
        await client.recordings(6, 0, 10)
    assert [r.id for r in await client.recordings(6, 0, 10)] == [1]


async def test_recordings_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import synology_ss_playback.client as client_mod

    monkeypatch.setattr(client_mod, "RECORDINGS_CACHE_ENTRIES", 2)
    client, session = _client([_resp(body=_json(_LOGIN)), _events(1), _events(2), _events(3), _events(4)])
    for end in (10, 11, 12):
        await client.recordings(6, 0, end)
    assert [r.id for r in await client.recordings(6, 0, 12)] == [3]  # kept
    assert [r.id for r in await client.recordings(6, 0, 10)] == [4]  # the oldest went
    assert session.request.call_count == 5
