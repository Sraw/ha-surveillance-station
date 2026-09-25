"""SurveillanceStationClient: login, session-error retries, and the API calls."""

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
        ]
    )
    await client.login()
    await client.login(stale_sid="old")
    assert client._sid == "new"
    assert session.request.call_count == 2


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
            _resp(body=_json({"error": {"code": 400}})),  # still fails
        ]
    )
    with pytest.raises(SSError) as err:
        await client.download(1, 1, 0, 1000)
    assert not isinstance(err.value, SSAuthError)
    assert session.request.call_count == 4


async def test_call_retries_once_on_session_error() -> None:
    client, session = _client(
        [
            _resp(body=_json({"success": True, "data": {"sid": "s1"}})),
            _resp(body=_json({"success": False, "error": {"code": 105}})),
            _resp(body=_json({"success": True, "data": {"sid": "s2"}})),
            _resp(body=_json({"success": True, "data": {"cameras": []}})),
        ]
    )
    assert await client.cameras() == []
    assert session.request.call_count == 4


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
