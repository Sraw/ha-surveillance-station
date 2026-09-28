"""Time-lapse: the day index, sessions (one at a time), and their transcoded segments."""

import asyncio
from datetime import datetime
from http import HTTPStatus
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator
from synology_ss_playback import SSConnectionError, SSError, TimelapseRecording, TranscodeSpec

from custom_components.surveillance_station.const import CONF_TRANSCODER
from custom_components.surveillance_station.views import DATA_MANAGER
from homeassistant.core import HomeAssistant

from .conftest import T0

TZ = ZoneInfo("US/Pacific")
MID = int(datetime(2026, 9, 22, tzinfo=TZ).timestamp())  # midnight of 2026-09-22, NAS time
FETCH = "custom_components.surveillance_station.views.fetch_timelapse_segment"
HW_CHECK = "custom_components.surveillance_station.views.hardware_transcode_available"


def _file(**kw) -> TimelapseRecording:
    base = dict(
        id=1, camera_id=6, task_id=3, start=MID - 6 * 3600, span=86400, frames=10800,
        width=4512, height=2512, hevc=True, live=False,
    )
    base.update(kw)
    return TimelapseRecording(**base)


# 09-21 18:00 -> 09-22 18:00 (240x), then one still being written from 18:00 (90 s of video = 6 h).
FILES = [
    _file(),
    _file(id=2, start=MID + 18 * 3600, span=6 * 3600, frames=2700, live=True),
    _file(id=3, camera_id=7, start=MID, span=86400, frames=10800, width=2560, height=1440),
]


@pytest.fixture
def timelapse(mock_client: MagicMock):
    mock_client.timelapse_recordings = AsyncMock(return_value=list(FILES))
    mock_client.timezone = AsyncMock(return_value=TZ)
    with patch(HW_CHECK, AsyncMock(return_value=True)) as hw:
        yield hw


async def _ws(hass: HomeAssistant, hass_ws_client: WebSocketGenerator, **msg) -> dict:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id(msg)
    return await ws.receive_json()


async def test_days(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator
) -> None:
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days")
    assert msg["success"]
    res = msg["result"]
    # The GPU check is started, not waited for; the next listing has its answer.
    assert res["timezone"] == "US/Pacific" and res["hardware"] is None
    await hass.async_block_till_done()
    assert (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days"))["result"]["hardware"] is True
    assert [(c["id"], c["name"]) for c in res["cameras"]] == [(7, "Backyard"), (6, "Drive Way")]
    days = res["cameras"][1]["days"]
    assert [d["date"] for d in days] == ["2026-09-22", "2026-09-21"]
    # The two files join up; the live one stops 2 video seconds (8 min) short of what SS has.
    assert days[0] == {
        "date": "2026-09-22", "start": MID, "end": MID + 86400, "covered": [[MID, MID + 85920]],
    }
    assert days[1]["covered"] == [[MID - 6 * 3600, MID]]


async def test_days_skip_a_sliver_and_use_the_list_cache(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, timelapse,
    hass_ws_client: WebSocketGenerator,
) -> None:
    mock_client.timelapse_recordings.return_value = [_file(start=MID + 3600, span=0, frames=8)]
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days")
    assert msg["result"]["cameras"] == [{"id": 6, "name": "Drive Way", "days": []}]
    await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days")
    assert mock_client.timelapse_recordings.await_count == 1
    timelapse.assert_awaited_once()  # the GPU is checked once


async def test_days_skip_a_day_with_less_than_a_video_second(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, timelapse,
    hass_ws_client: WebSocketGenerator,
) -> None:
    # Ends 3 minutes (0.75 video s) past midnight: the day before plays it to its end.
    mock_client.timelapse_recordings.return_value = [_file(start=MID + 180 - 86400)]
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days")
    assert [d["date"] for d in msg["result"]["cameras"][0]["days"]] == ["2026-09-21"]


async def test_days_surveillance_station_error(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, timelapse,
    hass_ws_client: WebSocketGenerator,
) -> None:
    mock_client.timelapse_recordings.side_effect = SSConnectionError("SYNO.SurveillanceStation.TimeLapse.Recording", "List", None)
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days")
    assert not msg["success"] and msg["error"]["code"] == "surveillance_station_error"


async def test_session_playlist_and_segments(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse,
    hass_ws_client: WebSocketGenerator, hass_client_no_auth: ClientSessionGenerator,
) -> None:
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22")
    assert msg["success"]
    res = msg["result"]
    assert res["codec"] == "hevc" and res["hardware"] is True
    assert (res["start"], res["end"]) == (MID, MID + 86400)
    # File 1 from its 90th video second (midnight) to its end, then file 2 to its margin.
    assert [(r["media_start"], r["duration"], r["wall_start"], r["rate"]) for r in res["runs"]] == [
        (0.0, 270, MID, 240.0), (270.0, 88, MID + 18 * 3600, 240.0)
    ]
    assert res["duration"] == 358
    assert res["segments"][:3] == [0, 2, 6]  # 90 s -> the 92 s grid line
    assert res["maps"] == [{"index": 0, "size": "1280x712"}, {"index": 68, "size": "1280x712"}]

    client = await hass_client_no_auth()
    resp = await client.get(res["url"])
    assert resp.status == HTTPStatus.OK
    playlist = await resp.text()
    assert playlist.count("#EXTINF:") == len(res["segments"]) and "#EXT-X-DISCONTINUITY" not in playlist

    base = res["url"].rsplit("/", 1)[0]
    fetch = AsyncMock(return_value=(b"init", b"media"))
    with patch(FETCH, fetch):
        seg = await client.get(f"{base}/seg/68.m4s")
        init = await client.get(f"{base}/init/68.mp4")
    assert (await seg.read(), await init.read()) == (b"media", b"init")
    fetch.assert_awaited_once()
    _, seg_arg, _, spec = fetch.await_args.args
    assert (seg_arg.recording_id, seg_arg.offset_ms, seg_arg.media_start) == (2, 0, 270.0)
    assert spec == TranscodeSpec("hevc", True, True, 1280, 712)
    assert hass.data[DATA_MANAGER].stats()["timelapse_transcoded"] == 1


async def test_session_without_a_gpu_is_h264(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timelapse.return_value = False
    with caplog.at_level(logging.INFO):
        msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=7, date="2026-09-22")
    assert msg["result"]["codec"] == "h264" and msg["result"]["hardware"] is False
    assert msg["result"]["maps"] == [{"index": 0, "size": "1280x720"}]
    assert "none usable" in caplog.text


def _transcoder(hass: HomeAssistant, entry: MockConfigEntry, mode: str) -> None:
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_TRANSCODER: mode})


async def test_session_on_the_cpu_by_choice(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator
) -> None:
    _transcoder(hass, setup_integration, "cpu")
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22")
    assert msg["result"]["codec"] == "h264" and msg["result"]["hardware"] is False
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days")
    assert msg["result"]["hardware"] is False
    timelapse.assert_not_awaited()  # the GPU isn't even checked


@pytest.mark.parametrize(
    ("found", "error"),
    [
        (True, None),
        (False, "not_supported"),  # no GPU: no time-lapse rather than the CPU
        (None, "gpu_busy"),  # busy, not missing: try again
    ],
)
async def test_session_gpu_only(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator,
    found: bool | None, error: str | None,
) -> None:
    _transcoder(hass, setup_integration, "gpu")
    timelapse.return_value = found
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22")
    if error is None:
        assert msg["result"]["codec"] == "hevc" and msg["result"]["hardware"] is True
    else:
        assert not msg["success"] and msg["error"]["code"] == error
    if found is False:
        assert "GPU only" in msg["error"]["message"]
    if found is None:
        msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days")
        assert msg["success"] and msg["result"]["hardware"] is None


async def test_session_nothing_that_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator
) -> None:
    timelapse.return_value = None  # the GPU busy: no matter, there is nothing to transcode
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-08-01")
    assert msg["result"]["url"] is None and msg["result"]["runs"] == [] and msg["result"]["duration"] == 0
    timelapse.assert_not_awaited()


@pytest.mark.parametrize("date", ["2026-13-40", "yesterday", "2026-09-22T00"])
async def test_session_bad_date(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator, date: str
) -> None:
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date=date)
    assert not msg["success"] and msg["error"]["code"] == "invalid_format"


async def test_one_session_at_a_time(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse,
    hass_ws_client: WebSocketGenerator, hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A newer time-lapse ends the older one: 410 (not 404, which would make its player reopen)."""
    first = (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22"))["result"]
    vod = await _ws(hass, hass_ws_client, type="surveillance_station/vod", camera_id=6, start=T0, end=T0 + 60)
    second = (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=7, date="2026-09-22"))["result"]
    client = await hass_client_no_auth()
    base = first["url"].rsplit("/", 1)[0]
    assert (await client.get(first["url"])).status == HTTPStatus.GONE
    assert (await client.get(f"{base}/seg/0.m4s")).status == HTTPStatus.GONE
    assert (await client.get(second["url"])).status == HTTPStatus.OK
    # Recordings' sessions aren't time-lapse ones: untouched.
    assert (await client.get(vod["result"]["url"])).status == HTTPStatus.OK
    assert (await client.get(f"{base.rsplit('/', 1)[0]}/nope/index.m3u8")).status == HTTPStatus.NOT_FOUND


async def test_superseded_while_queued_is_gone(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse,
    hass_ws_client: WebSocketGenerator, hass_client_no_auth: ClientSessionGenerator,
) -> None:
    first = (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22"))["result"]
    base = first["url"].rsplit("/", 1)[0]
    gate = asyncio.Event()
    started: list[int] = []

    async def slow(client, seg, *args, **kwargs):
        started.append(seg.index)
        await gate.wait()
        return b"i", b"m"

    client = await hass_client_no_auth()
    with patch(FETCH, AsyncMock(side_effect=slow)):
        # Fill the transcode slots, then queue one more behind them.
        busy = [asyncio.create_task(client.get(f"{base}/seg/{i}.m4s")) for i in range(3)]
        while len(started) < 3:
            await asyncio.sleep(0.01)
        queued = asyncio.create_task(client.get(f"{base}/seg/10.m4s"))
        await asyncio.sleep(0.05)
        await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=7, date="2026-09-22")
        assert (await queued).status == HTTPStatus.GONE
        gate.set()
        # Those under way go too (their transcodes, once running, are the
        # library's to finish: see synology_ss/tests).
        assert [(await t).status for t in busy] == [HTTPStatus.GONE] * 3


async def test_segment_failures(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse,
    hass_ws_client: WebSocketGenerator, hass_client_no_auth: ClientSessionGenerator,
) -> None:
    res = (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22"))["result"]
    base = res["url"].rsplit("/", 1)[0]
    client = await hass_client_no_auth()
    with patch(FETCH, AsyncMock(side_effect=SSError("ffmpeg", "run", 1, "boom"))):
        assert (await client.get(f"{base}/seg/1.m4s")).status == HTTPStatus.BAD_GATEWAY
    with patch(FETCH, AsyncMock(side_effect=SSConnectionError("SYNO.SurveillanceStation.Recording", "Download", None))):
        assert (await client.get(f"{base}/seg/2.m4s")).status == HTTPStatus.BAD_GATEWAY
    assert hass.data[DATA_MANAGER].stats()["timelapse_transcode_failures"] == 2


async def test_superseded_tokens_are_bounded(hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse) -> None:
    from custom_components.surveillance_station.const import VOD_MAX_SESSIONS
    from custom_components.surveillance_station.views import VodSession

    manager = hass.data[DATA_MANAGER]
    for _ in range(VOD_MAX_SESSIONS + 5):
        manager.create_timelapse_session(VodSession("e", 6, 0, [], 1e12, transcode={1: None}))
    assert len(manager.superseded) == VOD_MAX_SESSIONS


async def test_dst_days_are_23_and_25_hours(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, timelapse,
    hass_ws_client: WebSocketGenerator,
) -> None:
    spring = int(datetime(2026, 3, 8, tzinfo=TZ).timestamp())
    fall = int(datetime(2026, 11, 1, tzinfo=TZ).timestamp())
    mock_client.timelapse_recordings.return_value = [_file(start=spring, span=3 * 86400), _file(id=9, start=fall, span=3 * 86400)]
    days = (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days"))["result"]["cameras"][0]["days"]
    by = {d["date"]: d for d in days}
    assert by["2026-03-08"]["end"] - by["2026-03-08"]["start"] == 23 * 3600
    assert by["2026-11-01"]["end"] - by["2026-11-01"]["start"] == 25 * 3600


async def test_one_task_per_camera(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock, timelapse,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Two tasks of a camera: the one it recorded with last, not both stitched together."""
    mock_client.timelapse_recordings.return_value = [_file(task_id=1), _file(id=5, task_id=2, start=MID, span=3600, frames=450)]
    res = (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22"))["result"]
    assert [r["duration"] for r in res["runs"]] == [15]


async def test_date_out_of_range(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator
) -> None:
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="9999-12-31")
    assert not msg["success"] and msg["error"]["code"] == "invalid_format"


async def test_vod_runs_refuses_a_timelapse_token(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator
) -> None:
    res = (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22"))["result"]
    token = res["url"].rsplit("/", 2)[1]
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/vod_runs", token=token)
    assert not msg["success"] and "time-lapse" in msg["error"]["message"]


async def test_gpu_check_undecided_is_retried(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timelapse.return_value = None  # timed out: the GPU busy, not missing
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22")
    assert not msg["success"] and msg["error"]["code"] == "gpu_busy"  # not the CPU instead
    assert "timed out" in caplog.text
    msg = await _ws(hass, hass_ws_client, type="surveillance_station/timelapse_days")
    assert msg["success"] and msg["result"]["hardware"] is None
    assert timelapse.await_count == 2  # asked again each time
    timelapse.return_value = True
    manager = hass.data[DATA_MANAGER]
    assert await manager.hardware() is True
    assert await manager.hardware() is True
    assert timelapse.await_count == 3  # then settled


async def test_running_transcodes_are_drained_on_stop(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    with patch("custom_components.surveillance_station.drain_transcodes", AsyncMock()) as drain:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    drain.assert_awaited_once()


def _like_the_library(transcoding: asyncio.Event, started: list[int], on_gpu: list[int]):
    """fetch_timelapse_segment's shape: download, then the transcode under the GPU lock,
    shielded (it finishes whoever leaves) and freeing the lock when it ends."""

    async def fetch(client, seg, ffmpeg, spec, gpu):
        started.append(seg.index)
        await gpu.acquire()
        on_gpu.append(seg.index)

        async def transcode() -> tuple[bytes, bytes]:
            try:
                await transcoding.wait()
            finally:
                gpu.release()
            return b"i", b"m%d" % seg.index

        return await asyncio.shield(asyncio.ensure_future(transcode()))

    return fetch


async def _timelapse_session(hass: HomeAssistant, hass_ws_client: WebSocketGenerator):
    res = (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22"))["result"]
    return hass.data[DATA_MANAGER].get_session(res["url"].rsplit("/", 2)[1])


async def test_one_transcode_at_a_time_on_the_gpu(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator
) -> None:
    session = await _timelapse_session(hass, hass_ws_client)
    manager = hass.data[DATA_MANAGER]
    transcoding = asyncio.Event()
    started: list[int] = []
    on_gpu: list[int] = []
    with patch(FETCH, AsyncMock(side_effect=_like_the_library(transcoding, started, on_gpu))):
        jobs = [asyncio.create_task(manager.fetch(session, s)) for s in session.segments[:3]]
        while len(started) < 3:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert len(on_gpu) == 1  # three cuts downloaded, one of them on the GPU
        transcoding.set()
        await asyncio.gather(*jobs)
    assert sorted(on_gpu) == [0, 1, 2]


async def test_running_transcode_keeps_its_slot_and_result(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse, hass_ws_client: WebSocketGenerator
) -> None:
    """Left by everyone while on the GPU: the transcode goes on (and holds its cut), so no
    other download starts in its place, and what it made is kept."""
    session = await _timelapse_session(hass, hass_ws_client)
    manager = hass.data[DATA_MANAGER]
    transcoding = asyncio.Event()
    started: list[int] = []
    on_gpu: list[int] = []
    fetch = AsyncMock(side_effect=_like_the_library(transcoding, started, on_gpu))
    with patch(FETCH, fetch):
        first = asyncio.create_task(manager.fetch(session, session.segments[0]))
        while not on_gpu:
            await asyncio.sleep(0.01)
        first.cancel()
        others = [asyncio.create_task(manager.fetch(session, s)) for s in session.segments[1:4]]
        await asyncio.sleep(0.05)
        assert started == [0, 1, 2]  # MAX_PARALLEL_TRANSCODES, the first one's still among them
        transcoding.set()
        await asyncio.gather(*others)
        assert started == [0, 1, 2, 3]
        assert await manager.fetch(session, session.segments[0]) == (b"i", b"m0")
    assert fetch.await_count == 4


async def test_segments_answer_home_assistant_cast(
    hass: HomeAssistant, setup_integration: MockConfigEntry, timelapse,
    hass_ws_client: WebSocketGenerator, hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The card on HA Cast fetches from another origin: CORS as HA's http integration
    allows it (Cast by default), no other origin."""
    res = (await _ws(hass, hass_ws_client, type="surveillance_station/timelapse", camera_id=6, date="2026-09-22"))["result"]
    base = res["url"].rsplit("/", 1)[0]
    client = await hass_client_no_auth()
    cast = "https://cast.home-assistant.io"
    with patch(FETCH, AsyncMock(return_value=(b"init", b"media"))):
        for path in ("seg/0.m4s", "init/0.mp4"):
            resp = await client.get(f"{base}/{path}", headers={"Origin": cast})
            assert resp.status == HTTPStatus.OK and resp.headers["Access-Control-Allow-Origin"] == cast
            resp = await client.get(f"{base}/{path}", headers={"Origin": "https://example.com"})
            assert "Access-Control-Allow-Origin" not in resp.headers


async def test_timelapse_lists_per_entry(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """One NAS that hangs listing its files holds up only its own time-lapse."""
    manager = hass.data[DATA_MANAGER]
    hung, fine = MagicMock(), MagicMock()
    hung.timelapse_recordings = AsyncMock(side_effect=lambda: asyncio.Event().wait())
    fine.timelapse_recordings = AsyncMock(return_value=list(FILES))
    stuck = asyncio.create_task(manager.timelapse_files("A", hung))
    await asyncio.sleep(0.01)
    assert await asyncio.wait_for(manager.timelapse_files("B", fine), 1) == FILES
    stuck.cancel()
