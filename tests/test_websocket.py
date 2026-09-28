"""WebSocket commands and the HLS views they hand out URLs for."""

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator
from synology_ss_playback import Bookmark, RecordingInfo, SSConnectionError, SSError

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
        "search": False,  # no Frigate URL
    }


async def test_bookmarks_in_range(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, hass_ws_client: WebSocketGenerator
) -> None:
    """The timeline's window: overlapping bookmarks, oldest first, of the cameras asked for."""
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/bookmarks", "start": T0, "end": T0 + 1000})
    msg = await ws.receive_json()
    assert msg["success"]
    assert [b["id"] for b in msg["result"]["bookmarks"]] == [1, 2]
    bookmark = msg["result"]["bookmarks"][0]
    assert bookmark == {
        "id": 1, "camera_id": 6, "name": "person", "comment": "", "start": T0 + 60, "end": T0 + 70,
    }

    await ws.send_json_auto_id(
        {"type": "surveillance_station/bookmarks", "camera_ids": [7], "start": T0, "end": T0 + 3600}
    )
    assert [b["id"] for b in (await ws.receive_json())["result"]["bookmarks"]] == [3]
    # One SS round trip for both: the list is cached briefly and filtered here.
    assert mock_client.list_bookmarks.await_count == 1
    mock_client.list_bookmarks.assert_awaited_with([6, 7])


async def test_unreachable(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, hass_ws_client: WebSocketGenerator
) -> None:
    mock_client.cameras.side_effect = SSConnectionError("SYNO.SurveillanceStation.Camera", "List", None)
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/cameras"})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"] == {"code": "surveillance_station_error", "message": "Surveillance Station is unreachable"}


async def test_no_entry_id_and_nothing_loaded(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    """No entry_id given and no Surveillance Station is set up at all."""
    from custom_components.surveillance_station.const import DOMAIN
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DOMAIN, {})
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/cameras"})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"] == {"code": "not_found", "message": "no Surveillance Station is set up"}


async def test_unknown_entry_id(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/cameras", "entry_id": "not-a-real-entry-id"})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"] == {"code": "not_found", "message": "Surveillance Station entry not-a-real-entry-id is not loaded"}


async def test_a_bug_is_not_reported_as_bad_input(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A KeyError from the code (or the library) reaches HA's handler, which logs it."""
    mock_client.cameras.side_effect = KeyError("newName")
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/cameras"})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "unknown_error"
    assert "Error handling message" in caplog.text and "KeyError: 'newName'" in caplog.text


async def test_a_value_error_from_a_bug_is_not_invalid_format(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the commands' own refusals (InvalidRequest) are invalid_format; any other ValueError is logged."""
    mock_client.recordings.side_effect = ValueError("invalid literal for int()")
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/recordings", "camera_id": 6, "start": T0, "end": T0 + 60})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "unknown_error"
    assert "Error handling message" in caplog.text and "ValueError: invalid literal for int()" in caplog.text


async def test_generic_ss_error_is_reported_verbatim(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, hass_ws_client: WebSocketGenerator
) -> None:
    """Unlike a connection error, a generic SSError's text (API + code) is sent as is."""
    mock_client.cameras.side_effect = SSError("SYNO.SurveillanceStation.Camera", "List", 119)
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/cameras"})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "surveillance_station_error"
    assert "119" in msg["error"]["message"]


async def test_invalid_range(hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/recordings", "camera_id": 6, "start": T0, "end": T0})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"


@pytest.mark.parametrize("bad", ["inf", "-inf", "1e999", "nan", -1])
async def test_times_must_be_epoch_seconds(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator, bad: str | int
) -> None:
    """Refused as bad input (vol.Coerce(float) alone takes "inf", and int() of it raises OverflowError)."""
    ws = await hass_ws_client(hass)
    for kind, extra in (("recordings", {"camera_id": 6}), ("vod", {"camera_id": 6}), ("bookmarks", {})):
        for times in ({"start": bad, "end": T0}, {"start": T0, "end": bad}):
            await ws.send_json_auto_id({"type": f"surveillance_station/{kind}", **extra, **times})
            assert (await ws.receive_json())["error"]["code"] == "invalid_format", (kind, times)


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


async def test_vod_window_in_the_future_is_rejected(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    ws = await hass_ws_client(hass)
    future = T0 + 10_000_000  # long after "now" (mocked recordings ignore it anyway)
    with patch("custom_components.surveillance_station.websocket.time.time", return_value=T0):
        await ws.send_json_auto_id({"type": "surveillance_station/vod", "camera_id": 6, "start": future, "end": future + 60})
        msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"


async def test_vod_no_recordings_in_window_returns_no_url(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, hass_ws_client: WebSocketGenerator
) -> None:
    mock_client.recordings.return_value = []
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/vod", "camera_id": 6, "start": T0, "end": T0 + 60})
    msg = await ws.receive_json()
    assert msg["success"]
    assert msg["result"] == {"url": None, "runs": [], "start": float(T0), "end": float(T0 + 60), "live": False}


async def test_vod_live_window(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: MagicMock,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A window reaching the present is live: from a whole second, up to the live edge (not
    to now, nor to the end asked for), a playlist that grows."""
    now = T0 + 3600.7  # the live edge: the last 10 s grid line 5 s behind, T0 + 3590

    def recording(end: float) -> list[RecordingInfo]:
        return [RecordingInfo(id=101, camera_id=6, start=T0 + 1800, end=end, mount_id=1, live=True, hevc=True)]

    mock_client.recordings.return_value = recording(now)
    ws = await hass_ws_client(hass)
    client = await hass_client_no_auth()
    with patch("custom_components.surveillance_station.websocket.time.time") as clock:
        clock.return_value = now
        await ws.send_json_auto_id(
            {"type": "surveillance_station/vod", "camera_id": 6, "start": T0 + 3500.4, "end": T0 + 3700}
        )
        res = (await ws.receive_json())["result"]
        assert (res["live"], res["start"], res["end"]) == (True, T0 + 3500, T0 + 3590)
        assert res["runs"] == [{"wall_start": T0 + 3500, "media_start": 0.0, "duration": 90.0}]
        playlist = await (await client.get(res["url"])).text()
        assert "#EXT-X-PLAYLIST-TYPE:EVENT" in playlist and "#EXT-X-ENDLIST" not in playlist
        assert playlist.count("#EXTINF:") == 9

        # 20 s later: two more segments, though the end asked for is still ahead.
        clock.return_value = now + 20
        mock_client.recordings.return_value = recording(now + 20)
        playlist = await (await client.get(res["url"])).text()
        assert playlist.count("#EXTINF:") == 11
        token = res["url"].rsplit("/", 2)[1]
        await ws.send_json_auto_id({"type": "surveillance_station/vod_runs", "token": token})
        assert (await ws.receive_json())["result"]["runs"][0]["duration"] == 110.0

        # Starting over a day ago: never live, and a day long at most.
        await ws.send_json_auto_id(
            {"type": "surveillance_station/vod", "camera_id": 6, "start": now - 90_000, "end": now}
        )
        res = (await ws.receive_json())["result"]
        assert (res["live"], res["start"], res["end"]) == (False, T0 - 86_400, T0)


async def test_vod_runs(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/vod", "camera_id": 6, "start": T0, "end": T0 + 60})
    created = (await ws.receive_json())["result"]
    token = created["url"].rsplit("/", 2)[1]

    await ws.send_json_auto_id({"type": "surveillance_station/vod_runs", "token": token})
    msg = await ws.receive_json()
    assert msg["success"]
    assert msg["result"]["runs"] == created["runs"]

    # As the card sends it when its config names the entry.
    await ws.send_json_auto_id(
        {"type": "surveillance_station/vod_runs", "token": token, "entry_id": setup_integration.entry_id}
    )
    msg = await ws.receive_json()
    assert msg["success"]
    assert msg["result"]["runs"] == created["runs"]


async def test_vod_runs_unknown_token(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/vod_runs", "token": "not-a-real-token"})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"] == {"code": "not_found", "message": "playback session expired"}


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


async def test_bookmark_page(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Newest first, paged by a cursor."""
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page", "limit": 2})
    first = (await ws.receive_json())["result"]
    assert [b["id"] for b in first["bookmarks"]] == [3, 2]
    assert (first["total"], first["more"]) == (3, True)

    last = first["bookmarks"][-1]
    await ws.send_json_auto_id(
        {"type": "surveillance_station/bookmark_page", "limit": 2, "before": last["start"], "before_id": last["id"]}
    )
    second = (await ws.receive_json())["result"]
    assert [b["id"] for b in second["bookmarks"]] == [1]
    assert second["more"] is False

    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page", "camera_ids": [6]})
    only6 = (await ws.receive_json())["result"]
    assert ([b["id"] for b in only6["bookmarks"]], only6["total"]) == ([2, 1], 2)

    for bad in ({"limit": 1000}, {"before": T0}):
        await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page", **bad})
        assert (await ws.receive_json())["error"]["code"] == "invalid_format"


async def test_bookmark_page_kinds(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator, mock_client
) -> None:
    """Only the kinds asked for (a part of the name, any case); the kinds there are to choose, most common first."""
    names = ["Person", "Person, Car", "Car", "Animal", "My mark", "car"]  # "car" counts as Car
    mock_client.list_bookmarks.return_value = [
        Bookmark(id=10 - i, camera_id=6, name=n, comment="", start=T0 + 100 * (10 - i), end=T0 + 100 * (10 - i) + 5)
        for i, n in enumerate(names)
    ]
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page", "kinds": ["car"]})
    res = (await ws.receive_json())["result"]
    assert [b["name"] for b in res["bookmarks"]] == ["Person, Car", "Car", "car"] and res["total"] == 3
    # Counted over every bookmark of the cameras (not just the kinds shown), any case, as mostly written;
    # one-offs aren't kinds.
    assert res["kinds"] == [["Car", 3], ["Person", 2]]
    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page", "kinds": [" Person ", "Animal"]})
    res = (await ws.receive_json())["result"]
    assert [b["name"] for b in res["bookmarks"]] == ["Person", "Person, Car", "Animal"]
    assert res["kinds"] == [["Car", 3], ["Person", 2]]
    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page", "kinds": []})
    assert (await ws.receive_json())["result"]["total"] == 6


async def test_window_capped(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    ws = await hass_ws_client(hass)
    for kind in ("bookmarks", "recordings"):
        extra = {"camera_id": 6} if kind == "recordings" else {}
        await ws.send_json_auto_id(
            {"type": f"surveillance_station/{kind}", "start": T0, "end": T0 + 9 * 86400, **extra}
        )
        assert (await ws.receive_json())["error"]["code"] == "invalid_format"


async def test_thumbnail(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Signed URLs load without a header; the frame is fetched once, then cached."""
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page"})
    url = (await ws.receive_json())["result"]["bookmarks"][0]["thumbnail"]
    client = await hass_client_no_auth()

    snap = AsyncMock(return_value=b"\xff\xd8jpeg")
    with patch("custom_components.surveillance_station.views.fetch_snapshot", snap):
        first = await client.get(url)
        second = await client.get(url)
    assert first.status == HTTPStatus.OK
    assert first.content_type == "image/jpeg"
    assert await second.read() == b"\xff\xd8jpeg"
    assert snap.await_count == 1
    assert snap.await_args.args[1:3] == (7, T0 + 2001)


async def test_thumbnail_bad_signature_is_404(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Never 401: HA counts those as failed logins and bans the IP."""
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page"})
    url = (await ws.receive_json())["result"]["bookmarks"][0]["thumbnail"]
    path, query = url.split("?")
    exp = query.split("&")[0].split("=")[1]
    client = await hass_client_no_auth()
    other_ts = path.replace(f"/{T0 + 2001}.jpg", f"/{T0 + 5}.jpg")
    later = query.replace(f"exp={exp}", f"exp={int(exp) + 3600}")
    snap = AsyncMock(return_value=b"\xff\xd8jpeg")
    with patch("custom_components.surveillance_station.views.fetch_snapshot", snap):
        for bad in (
            path, f"{other_ts}?{query}", f"{path}?{later}", f"{path}?exp={exp}&sig=00",
            f"{path}?exp=%C2%B2&sig=00", f"{path}?exp={exp}&sig=%C3%A9",  # non-ASCII: 404, not 500
        ):
            assert (await client.get(bad)).status == HTTPStatus.NOT_FOUND, bad
        # Expired, e.g. a card left open for days.
        with patch("custom_components.surveillance_station.views.time.time", return_value=int(exp) + 1):
            assert (await client.get(url)).status == HTTPStatus.NOT_FOUND
    snap.assert_not_awaited()


async def test_thumbnail_nothing_recorded(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page"})
    url = (await ws.receive_json())["result"]["bookmarks"][0]["thumbnail"]
    client = await hass_client_no_auth()
    with patch("custom_components.surveillance_station.views.fetch_snapshot", AsyncMock(return_value=None)):
        assert (await client.get(url)).status == HTTPStatus.NOT_FOUND


async def test_thumbnail_surveillance_station_unreachable(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page"})
    url = (await ws.receive_json())["result"]["bookmarks"][0]["thumbnail"]
    client = await hass_client_no_auth()
    down = AsyncMock(side_effect=SSConnectionError("SYNO.SurveillanceStation.Recording", "Download", None))
    with patch("custom_components.surveillance_station.views.fetch_snapshot", down):
        assert (await client.get(url)).status == HTTPStatus.BAD_GATEWAY


async def test_thumbnail_surveillance_station_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/bookmark_page"})
    url = (await ws.receive_json())["result"]["bookmarks"][0]["thumbnail"]
    client = await hass_client_no_auth()
    failed = AsyncMock(side_effect=SSError("SYNO.SurveillanceStation.Recording", "Download", 119))
    with patch("custom_components.surveillance_station.views.fetch_snapshot", failed):
        assert (await client.get(url)).status == HTTPStatus.BAD_GATEWAY


async def test_vod_segment_surveillance_station_unreachable(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/vod", "camera_id": 6, "start": T0, "end": T0 + 60})
    base = (await ws.receive_json())["result"]["url"].rsplit("/", 1)[0]
    client = await hass_client_no_auth()
    down = AsyncMock(side_effect=SSConnectionError("SYNO.SurveillanceStation.Recording", "Download", None))
    with patch("custom_components.surveillance_station.views.fetch_segment", down):
        assert (await client.get(f"{base}/init/0.mp4")).status == HTTPStatus.BAD_GATEWAY


async def test_vod_segment_surveillance_station_error(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "surveillance_station/vod", "camera_id": 6, "start": T0, "end": T0 + 60})
    base = (await ws.receive_json())["result"]["url"].rsplit("/", 1)[0]
    client = await hass_client_no_auth()
    failed = AsyncMock(side_effect=SSError("SYNO.SurveillanceStation.Recording", "Download", 119))
    with patch("custom_components.surveillance_station.views.fetch_segment", failed):
        assert (await client.get(f"{base}/seg/0.m4s")).status == HTTPStatus.BAD_GATEWAY


async def test_recording_in_progress(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, hass_ws_client: WebSocketGenerator
) -> None:
    """Still live while SS keeps moving its end forward; stopped once it doesn't."""
    now = 2_000_000_000
    mock_client.recordings.return_value = [
        RecordingInfo(id=1, camera_id=6, start=now - 900, end=now - 8, mount_id=1, live=True, hevc=True),
        RecordingInfo(id=2, camera_id=6, start=now - 900, end=now - 40, mount_id=1, live=True, hevc=True),
    ]
    ws = await hass_ws_client(hass)
    with patch("custom_components.surveillance_station.websocket.time.time", return_value=now):
        await ws.send_json_auto_id(
            {"type": "surveillance_station/recordings", "camera_id": 6, "start": now - 3600, "end": now}
        )
        msg = await ws.receive_json()
    assert msg["success"]
    assert [r["live"] for r in msg["result"]["recordings"]] == [True, False]
