"""Time-lapse: listing, the streamed download, day planning, and the transcode glue.

ffmpeg itself is mocked (asyncio.create_subprocess_exec), as in test_segment.
"""

import asyncio
import json
import os
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from synology_ss_playback import (
    SSConnectionError,
    SSError,
    SurveillanceStationClient,
    TimelapseRecording,
    TranscodeSpec,
    covered,
    fetch_timelapse_segment,
    hardware_transcode_available,
    output_size,
    plan_day,
)
from synology_ss_playback import segment as seg_mod
from synology_ss_playback import timelapse as tl


def _rec(**kw) -> TimelapseRecording:
    # A day (86400 s) in 360 s of video: 240x.
    base = dict(
        id=1, camera_id=6, task_id=3, start=1000, span=86400, frames=10800,
        width=4512, height=2512, hevc=True, live=False,
    )
    base.update(kw)
    return TimelapseRecording(**base)


# ---- listing ---------------------------------------------------------------


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _json_resp(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status = 200
    resp.read = AsyncMock(return_value=json.dumps(data).encode())
    resp.headers = {"Content-Type": "application/json"}
    return resp


def _file(**kw) -> dict:
    base = {
        "id": 5, "cameraId": 6, "taskId": 3, "startTime": 1000, "rangeMinute": 1440, "frameCount": 10800,
        "imgWidth": 4512, "imgHeight": 2512, "video_type": 6, "recording": False,
    }
    base.update(kw)
    return base


async def test_timelapse_recordings_pages_and_skips_bad_entries() -> None:
    pages = [
        {"success": True, "data": {"total": 5, "events": [
            _file(id=7, startTime=2000), _file(id=6, markAsDel=True), _file(id=8, frameCount=0), {"id": "x"},
        ]}},
        {"success": True, "data": {"total": 5, "events": [
            _file(id=9, startTime=500, recording=True, video_type=1, taskId=None, imgWidth=None),
        ]}},
    ]
    session = MagicMock()
    calls = []

    def request(method, url, **kwargs):
        calls.append(kwargs["params"])
        return _Ctx(_json_resp(pages[len(calls) - 1]))

    session.request = MagicMock(side_effect=request)
    client = SurveillanceStationClient(session, "nas", 5000, False, "u", "p")
    client._sid = "sid"
    files = await client.timelapse_recordings()
    assert [f.id for f in files] == [9, 7]
    assert files[0] == TimelapseRecording(9, 6, 0, 500, 86400, 10800, 0, 2512, False, True)
    assert files[1].hevc and files[1].span == 86400 and not files[1].live
    assert [c["start"] for c in calls] == [0, 4]
    assert calls[0]["lapseId"] == -1 and calls[0]["api"] == "SYNO.SurveillanceStation.TimeLapse.Recording"


async def test_timelapse_recordings_stops_on_an_unreadable_total() -> None:
    session = MagicMock()
    session.request = MagicMock(return_value=_Ctx(_json_resp({"success": True, "data": {"total": "?", "events": [_file()]}})))
    client = SurveillanceStationClient(session, "nas", 5000, False, "u", "p")
    client._sid = "sid"
    assert [f.id for f in await client.timelapse_recordings()] == [5]
    assert session.request.call_count == 1


async def test_download_passes_rec_evt_type_for_timelapse() -> None:
    resp = MagicMock(status=200, headers={"Content-Type": "video/mp4"})
    resp.read = AsyncMock(return_value=b"mp4")
    session = MagicMock()
    session.request = MagicMock(return_value=_Ctx(resp))
    client = SurveillanceStationClient(session, "nas", 5000, False, "u", "p")
    client._sid = "sid"
    assert await client.download(3, 0, 1000, 4000, timelapse=True) == b"mp4"
    assert session.request.call_args.kwargs["params"]["recEvtType"] == 3
    await client.download(3, 0, 1000, 4000)
    assert "recEvtType" not in session.request.call_args.kwargs["params"]


# ---- download_to -----------------------------------------------------------


class _Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, n: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def iter_chunked(self, n: int):
        while self._chunks:
            yield self._chunks.pop(0)


def _stream(chunks: list[bytes], status: int = 200, content_type: str = "video/mp4") -> MagicMock:
    resp = MagicMock(status=status, headers={"Content-Type": content_type})
    resp.content = _Content(chunks)
    return resp


def _streaming_client(responses: list) -> tuple[SurveillanceStationClient, MagicMock]:
    it = iter(responses)

    def get(url, **kwargs):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return _Ctx(nxt)

    session = MagicMock()
    session.get = MagicMock(side_effect=get)
    client = SurveillanceStationClient(session, "nas", 5000, False, "u", "p")
    client._sid = "sid"
    return client, session


@pytest.fixture
def memfd():
    fd = os.memfd_create("t")
    yield fd
    os.close(fd)


def _content(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, 1 << 20)


async def test_download_to_streams_into_the_file(memfd: int) -> None:
    client, session = _streaming_client([_stream([b"ab", b"cd", b"ef"])])
    assert await client.download_to(memfd, 3, 1, 4000, 4000, timelapse=True) == 6
    assert _content(memfd) == b"abcdef"
    params = session.get.call_args.kwargs["params"]
    assert params["recEvtType"] == 3 and params["offsetTimeMs"] == 4000 and params["playTimeMs"] == 4000
    assert params["mountId"] == 1 and params["_sid"] == "sid"


async def test_download_to_without_timelapse_has_no_rec_evt_type(memfd: int) -> None:
    client, session = _streaming_client([_stream([b"x"])])
    await client.download_to(memfd, 3, 0, 0, 1000)
    assert "recEvtType" not in session.get.call_args.kwargs["params"]


async def test_download_to_logs_in_again_on_a_session_error(memfd: int) -> None:
    err = json.dumps({"success": False, "error": {"code": 119}}).encode()
    client, session = _streaming_client([_stream([err[:5], err[5:]], content_type="text/plain"), _stream([b"ok"])])

    async def login(stale_sid=None):
        client._sid = "new"

    client.login = AsyncMock(side_effect=login)
    assert await client.download_to(memfd, 3, 0, 0, 1000) == 2
    client.login.assert_awaited_once_with(stale_sid="sid")
    assert session.get.call_args.kwargs["params"]["_sid"] == "new"
    assert _content(memfd) == b"ok"


async def test_download_to_logs_in_first_without_a_session(memfd: int) -> None:
    client, _ = _streaming_client([_stream([b"ok"])])
    client._sid = None

    async def login(stale_sid=None):
        client._sid = "s"

    client.login = AsyncMock(side_effect=login)
    await client.download_to(memfd, 3, 0, 0, 1000)
    client.login.assert_awaited_once_with(stale_sid=None)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (json.dumps({"success": False, "error": {"code": 400}}).encode(), 400),
        (b"{not json", None),
        (b"[1]", None),
    ],
)
async def test_download_to_error_reply(memfd: int, body: bytes, code) -> None:
    client, _ = _streaming_client([_stream([body], content_type="application/json")])
    with pytest.raises(SSError) as exc:
        await client.download_to(memfd, 3, 0, 0, 1000)
    assert exc.value.code == code and exc.value.method == "Download"


async def test_download_to_http_error(memfd: int) -> None:
    client, _ = _streaming_client([_stream([], status=500)])
    with pytest.raises(SSError, match="HTTP 500"):
        await client.download_to(memfd, 3, 0, 0, 1000)


async def test_download_to_connection_error(memfd: int) -> None:
    client, _ = _streaming_client([aiohttp.ClientConnectionError()])
    with pytest.raises(SSConnectionError):
        await client.download_to(memfd, 3, 0, 0, 1000)


def test_write_all_retries_short_writes() -> None:
    from synology_ss_playback import client as client_mod

    written = []
    with patch.object(client_mod.os, "write", side_effect=lambda fd, v: written.append(bytes(v[:2])) or len(written[-1])):
        assert client_mod._write_all(9, b"abcde") == 5
    assert written == [b"ab", b"cd", b"e"]


# ---- planning --------------------------------------------------------------


def test_plan_day_cuts_a_file_at_midnights() -> None:
    # File from t=1000 covering a day; the "day" is [1000+43200, 1000+86400+...).
    rec = _rec()
    segs, runs = plan_day([rec], 1000 + 43200, 1000 + 2 * 86400)
    # Noon of the file = 180 s of video; to its end (360 s).
    assert runs == [tl.TimelapseRun(0.0, 180, 1000 + 43200, 240.0, 1)]
    assert segs[0].offset_ms == 180_000 and segs[0].media_start == 0 and segs[0].new_map
    assert all(s.duration == 4 for s in segs) and len(segs) == 45
    assert not any(s.new_map for s in segs[1:])
    assert segs[-1].offset_ms == 356_000 and segs[-1].media_start == 176
    assert segs[1].wall_start == 1000 + 43200 + 4 * 240


def test_plan_day_grid_is_the_files_not_the_days() -> None:
    # 1 s into the video: a 3 s segment up to the 4 s grid line, then 4 s ones.
    segs, _ = plan_day([_rec()], 1000 + 240, 1000 + 240 + 10 * 240)
    assert [(s.offset_ms, s.duration) for s in segs] == [(1000, 3.0), (4000, 4.0), (8000, 3.0)]
    assert [s.media_start for s in segs] == [0, 3, 7]


def test_plan_day_never_starts_before_midnight_and_days_join() -> None:
    # Midnight 100 s (0.42 video s) into a video second: the day starts at the
    # next second, not 2 minutes before midnight, and the day before ends there.
    midnight = 1000 + 43200 + 100
    _, today = plan_day([_rec()], midnight, midnight + 86400)
    _, before = plan_day([_rec()], midnight - 86400, midnight)
    assert today[0].wall_start >= midnight
    assert before[-1].wall_start + before[-1].duration * before[-1].rate == today[0].wall_start


def test_plan_day_overlap_at_midnight_never_replays_the_day_before() -> None:
    # a ends less than a video second past midnight; the day before played it
    # to there, so b (overlapping a) starts there too, not at midnight.
    a = _rec(id=1, start=0)
    midnight = 86400 - 100
    b = _rec(id=2, start=midnight - 190)  # its next whole second is between midnight and a's stop
    _, before = plan_day([a, b], midnight - 86400, midnight)
    _, today = plan_day([a, b], midnight, midnight + 86400)
    stop = before[-1].wall_start + before[-1].duration * before[-1].rate
    assert before[-1].recording_id == 1 and stop > midnight
    assert today[0].recording_id == 2 and today[0].wall_start >= stop


def test_plan_day_two_files_and_a_gap() -> None:
    a = _rec(id=1, start=0)
    b = _rec(id=2, start=86400 + 3600, span=3600, frames=450)  # an hour in 15 s
    segs, runs = plan_day([b, a], 43200, 86400 + 7200)
    assert [(r.recording_id, r.media_start, r.duration) for r in runs] == [(1, 0.0, 180), (2, 180.0, 15)]
    assert runs[1].wall_start == 86400 + 3600
    firsts = [s for s in segs if s.new_map]
    assert [(s.recording_id, s.index, s.media_start) for s in firsts] == [(1, 0, 0), (2, 45, 180)]
    assert not any(s.discontinuity for s in segs)
    assert [s.index for s in segs] == list(range(len(segs)))


def test_plan_day_overlapping_files_never_replay() -> None:
    a = _rec(id=1, start=0)
    b = _rec(id=2, start=86400 - 3 * 240)  # starts 3 video-seconds before a ends
    segs, runs = plan_day([a, b], 0, 2 * 86400)
    assert runs[0].duration == 360
    assert runs[1].wall_start >= 86400 - 120
    assert segs[[s.recording_id for s in segs].index(2)].offset_ms == 3000


def test_plan_day_unaligned_overlap_starts_after_the_last_frame_played() -> None:
    a = _rec(id=1, start=0)
    b = _rec(id=2, start=86400 - 3 * 240 + 100)  # 2.58 video seconds of overlap
    segs, runs = plan_day([a, b], 0, 2 * 86400)
    second = next(s for s in segs if s.recording_id == 2)
    assert second.offset_ms == 3000  # ceil, never round back into a's last frames
    assert runs[1].wall_start >= runs[0].wall_start + runs[0].duration * runs[0].rate


def test_plan_day_live_file_stops_short_of_its_end() -> None:
    rec = _rec(live=True, frames=300, span=2400)  # 10 s of video so far
    segs, runs = plan_day([rec], 0, 10**6)
    assert runs[0].duration == 10 - tl.TIMELAPSE_LIVE_MARGIN_SECONDS
    assert covered(rec) == (1000, 1000 + 8 * 240)


def test_plan_day_skips_empty_and_out_of_day_files() -> None:
    assert plan_day([_rec(span=0), _rec(frames=0)], 0, 10**6) == ([], [])
    assert plan_day([_rec()], 10**6, 2 * 10**6) == ([], [])
    # Less than a video second of the file on this day.
    assert plan_day([_rec()], 1000 + 86400 - 100, 10**6) == ([], [])


def test_covered_of_an_empty_file() -> None:
    assert covered(_rec(span=0)) == (1000, 1000)
    assert covered(_rec()) == (1000, 1000 + 86400)


def test_output_size() -> None:
    assert output_size(4512, 2512) == (1280, 712)
    assert output_size(2560, 1440) == (1280, 720)
    assert output_size(640, 360) == (640, 360)
    assert output_size(641, 361) == (640, 360)
    assert output_size(0, 0) == (1280, 720)


def test_transcode_args_hardware_hevc() -> None:
    args = tl.ffmpeg_transcode_args("ffmpeg", "/src", 4.0, 12.0, TranscodeSpec("hevc", True, True, 1280, 712))
    line = " ".join(args)
    assert "-hwaccel qsv -hwaccel_output_format qsv -c:v hevc_qsv -i /src" in line
    assert "scale_qsv=w=1280:h=712" in line and "-c:v hevc_qsv" in line.split("-i /src")[1]
    assert "-tag:v hvc1" in line and "-output_ts_offset 12.000" in line and "-t 4.000" in line
    assert "-an" in args and args[-1] == "pipe:1"


def test_transcode_args_hardware_h264_from_h264() -> None:
    line = " ".join(tl.ffmpeg_transcode_args("ffmpeg", "/src", 4, 0, TranscodeSpec("h264", True, False, 640, 360)))
    assert "-c:v h264_qsv -i /src" in line and "-tag:v" not in line


def test_transcode_args_software_is_h264() -> None:
    line = " ".join(tl.ffmpeg_transcode_args("ffmpeg", "/src", 4, 0, TranscodeSpec("h264", False, True, 1280, 712)))
    assert "qsv" not in line and "-c:v libx264" in line and "scale=1280:712" in line


# ---- incomplete frames -----------------------------------------------------


def _nal(kind: int, body: bytes = b"xy") -> bytes:
    unit = bytes([kind << 1, 1]) + body
    return len(unit).to_bytes(4, "big") + unit


def _full(payload: bytes) -> bytes:
    return b"\0\0\0\0" + payload


def _mp4(
    samples: list[list[int]], *, entry: str = "hvc1", co64: bool = False, audio_first: bool = False, fixed: bool = False
) -> bytes:
    """ftyp, mdat (two samples a chunk), moov; each sample a list of NAL unit types."""
    data = [b"".join(_nal(k) for k in s) for s in samples]
    ftyp = _box("ftyp", b"isom")
    first = len(ftyp) + 8
    offsets, pos = [], first
    for i in range(0, len(data), 2):
        offsets.append(pos)
        pos += sum(len(d) for d in data[i : i + 2])
    mdat = _box("mdat", b"".join(data))
    hvcc = _box("hvcC", bytes(21) + b"\xff" + bytes(2))
    stsd = _box("stsd", _full((1).to_bytes(4, "big") + (_box(entry, bytes(78) + hvcc) if entry else b"")))
    if fixed:  # every sample the same size: no table
        stsz = _box("stsz", _full(len(data[0]).to_bytes(4, "big") + len(data).to_bytes(4, "big")))
    else:
        stsz = _box("stsz", _full(bytes(4) + len(data).to_bytes(4, "big") + b"".join(len(d).to_bytes(4, "big") for d in data)))
    stsc = _box("stsc", _full((1).to_bytes(4, "big") + (1).to_bytes(4, "big") + (2).to_bytes(4, "big") + (1).to_bytes(4, "big")))
    width = 8 if co64 else 4
    co = _box("co64" if co64 else "stco", _full(len(offsets).to_bytes(4, "big") + b"".join(o.to_bytes(width, "big") for o in offsets)))
    stbl = _box("stbl", stsd + stsz + stsc + co)

    def trak(handler: bytes, stbl: bytes) -> bytes:
        hdlr = _box("hdlr", _full(bytes(4) + handler + bytes(12)))
        return _box("trak", _box("mdia", hdlr + _box("minf", stbl)))

    traks = trak(b"vide", stbl)
    if audio_first:
        traks = trak(b"soun", _box("stbl", b"")) + traks
    return ftyp + mdat + _box("moov", _box("mvhd", _full(bytes(96))) + traks)


def _broken(data: bytes) -> list[int]:
    return tl.broken_frames(tl.slice_types(lambda offset, n: data[offset : offset + n], len(data)) or [])


def test_broken_frames_finds_a_frame_missing_a_slice() -> None:
    frame = [32, 33, 34, 19, 19]  # VPS, SPS, PPS, two IDR slices
    samples = [frame, [32, 33, 34, 19], frame, frame, [19], frame]
    assert _broken(_mp4(samples)) == [1, 4]
    assert _broken(_mp4(samples, co64=True, audio_first=True)) == [1, 4]


def test_broken_frames_takes_a_slice_of_another_picture() -> None:
    frame = [32, 33, 34, 19, 19]
    assert _broken(_mp4([frame, [19, 1], frame, frame, [1, 19]])) == [1, 4]


def test_broken_frames_leaves_a_stream_with_p_frames_alone() -> None:
    # IDR then P frames (a GOP): only a missing slice counts.
    assert _broken(_mp4([[19, 19], [1, 1], [1, 1], [1], [1, 1], [19, 19]])) == [3]


def test_broken_frames_takes_the_usual_slice_count() -> None:
    f = [32, 33, 34, 19, 19]
    assert _broken(_mp4([f, f, f + [19], f, f, f])) == []  # one frame with a slice more: not the rest
    assert _broken(_mp4([f, f, f + [1], f, [19], f])) == [2, 4]


def test_broken_frames_takes_any_intra_type() -> None:
    assert _broken(_mp4([[19, 19], [21, 21], [21, 21], [20, 20], [19, 21], [16, 16]])) == [4]


def test_broken_frames_by_the_usual_frame() -> None:
    assert _broken(_mp4([[19, 19], [19], [19], [19, 19], [19]])) == []
    assert _broken(_mp4([[19, 19], [19, 19], [32], [19, 19]])) == [2]  # a frame without a slice


def test_broken_frames_none() -> None:
    assert _broken(_mp4([[32, 19, 19]] * 3)) == []
    assert _broken(_mp4([[32, 19, 19], [19, 19, 32]], fixed=True)) == []
    assert _broken(_mp4([[19, 19, 32], [19, 32, 32], [19, 19, 32]], fixed=True)) == [1]
    assert _broken(_mp4([[32, 19], [19]])) == []  # one slice a frame: nothing to miss
    assert _broken(_mp4([[19, 19], [19]], entry="avc1")) == []  # not H.265
    assert _broken(_mp4([[19, 19], [19]], entry="")) == []  # no sample entry


@pytest.mark.parametrize(
    "mangle",
    [
        lambda d: d.replace(  # the first run starts at chunk 2
            b"stsc" + bytes(4) + (1).to_bytes(4, "big") + (1).to_bytes(4, "big"),
            b"stsc" + bytes(4) + (1).to_bytes(4, "big") + (2).to_bytes(4, "big"),
        ),
        lambda d: d.replace(b"stsz" + bytes(8), b"stsz" + bytes(4) + (10**9).to_bytes(4, "big")),  # a count past its box
        lambda d: d.replace(b"stco" + bytes(4) + (1).to_bytes(4, "big"), b"stco" + bytes(4) + (10**9).to_bytes(4, "big")),
        lambda d: d.replace(b"stsz" + bytes(8), b"stsz" + bytes(4) + (10**9).to_bytes(4, "big"), 1).replace(
            b"stsz" + bytes(4) + (10**9).to_bytes(4, "big"), b"stsz" + (10**6).to_bytes(4, "big") + (10**6).to_bytes(4, "big")
        ),  # fixed sizes past the file
        lambda d: d.replace(  # one sample a chunk: the second sample in no chunk
            b"stsc" + bytes(4) + (1).to_bytes(4, "big") * 2 + (2).to_bytes(4, "big"),
            b"stsc" + bytes(4) + (1).to_bytes(4, "big") * 3,
        ),
    ],
)
def test_slice_types_of_a_malformed_file(mangle) -> None:
    good = _mp4([[19, 19], [19]])
    bad = mangle(good)
    assert bad != good
    assert tl.slice_types(lambda offset, n: bad[offset : offset + n], len(bad)) is None


def test_slice_types_with_an_offset_past_the_file() -> None:
    good = _mp4([[19, 19], [19]], co64=True)
    at = good.index(b"co64") + 12
    bad = good[:at] + (2**63).to_bytes(8, "big") + good[at + 8 :]

    def read(offset: int, n: int) -> bytes:
        if offset >= 2**62:
            raise OverflowError("signed integer is greater than maximum")
        return bad[offset : offset + n]

    assert tl.slice_types(read, len(bad)) is None


def test_broken_frames_of_something_else() -> None:
    good = _mp4([[19, 19], [19]])
    assert _broken(b"") == [] and _broken(b"not an mp4 at all") == []
    assert _broken(good[: len(good) - 10]) == []  # truncated moov
    no_video = _mp4([[19, 19]]).replace(b"vide", b"soun")
    assert _broken(no_video) == []
    bad_sample = bytearray(_mp4([[19, 19]] * 3))
    first = bad_sample.index(b"mdat") + 4
    bad_sample[first : first + 4] = (10_000).to_bytes(4, "big")  # a NAL running past its sample
    assert _broken(bytes(bad_sample)) == [0]  # a broken frame, not a file it can't read
    moov = good.index(b"moov") - 4
    large = (1).to_bytes(4, "big") + b"free" + (16).to_bytes(8, "big")  # a 64-bit box size
    assert _broken(good[:moov] + large + good[moov:]) == [1]
    assert _broken(good[:moov] + bytes(4) + good[moov + 4 :]) == [1]  # size 0: to the end of the file


# ---- hardware check --------------------------------------------------------


def _proc(returncode: int = 0, out: bytes = b"", err: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(out, err))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


@pytest.mark.parametrize(("rc", "want"), [(0, True), (1, False)])
async def test_hardware_transcode_available(rc: int, want: bool) -> None:
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_proc(rc))) as run:
        assert await hardware_transcode_available("ffmpeg") is want
    assert "hevc_qsv" in run.call_args.args


async def test_hardware_transcode_available_without_ffmpeg() -> None:
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())):
        assert await hardware_transcode_available("ffmpeg") is False


async def test_hardware_transcode_available_times_out() -> None:
    proc = _proc()
    calls = []

    async def wait():
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(10)
        return -9

    proc.wait = AsyncMock(side_effect=wait)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await hardware_transcode_available("ffmpeg", timeout=0.01) is None  # undecided
    proc.kill.assert_called_once()


# ---- fetch_timelapse_segment -----------------------------------------------


def _box(typ: str, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ.encode()) + payload


FMP4 = _box("ftyp", b"iso5") + _box("moov", b"x" * 8) + _box("moof", b"m") + _box("mdat", b"d" * 4)
HW = TranscodeSpec("hevc", True, True, 1280, 712)
SW = TranscodeSpec("h264", False, True, 1280, 712)


def _tseg():
    segs, _ = plan_day([_rec()], 1000, 1000 + 86400)
    return segs[1]


def _dl_client(data: bytes = b"cut") -> MagicMock:
    client = MagicMock()

    async def download_to(fd, *args, **kwargs):
        os.write(fd, data)
        return len(data)

    client.download_to = AsyncMock(side_effect=download_to)
    return client


@pytest.mark.parametrize("spec", [HW, SW])
async def test_fetch_timelapse_segment(spec: TranscodeSpec) -> None:
    client = _dl_client()
    seen = {}

    async def run(*argv, **kwargs):
        # ffmpeg reads the cut through the inherited descriptor.
        fd = kwargs["pass_fds"][0]
        os.lseek(fd, 0, os.SEEK_SET)
        seen["cut"] = os.read(fd, 100)
        seen["argv"] = argv
        return _proc(0, FMP4)

    seg = _tseg()
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=run)):
        init, media = await fetch_timelapse_segment(client, seg, "ffmpeg", spec, gpu=asyncio.Semaphore(1))
    assert init.startswith(_box("ftyp", b"iso5")) and media.startswith(_box("moof", b"m"))
    assert seen["cut"] == b"cut"
    assert "/proc/self/fd/" in seen["argv"][seen["argv"].index("-i") + 1]
    client.download_to.assert_awaited_once()
    args = client.download_to.await_args
    assert args.args[1:] == (seg.recording_id, 0, seg.offset_ms, 4000) and args.kwargs == {"timelapse": True}


WHOLE, HALF = (19, 19), (19,)
CUT_TYPES = [WHOLE, WHOLE, WHOLE, HALF, WHOLE, HALF]  # frames 3 and 5 miss a slice


def _types_before_after(after: list | None):
    """slice_types: the cut's frames first, then (the re-muxed cut) ``after``."""
    return patch.object(seg_mod, "slice_types", side_effect=[CUT_TYPES, after])


async def test_broken_frames_never_reach_the_gpu() -> None:
    gpu = asyncio.Semaphore(1)
    calls, closed = [], []
    real_close = os.close

    async def run(*argv, **kwargs):
        fds = kwargs["pass_fds"]
        os.lseek(fds[0], 0, os.SEEK_SET)
        calls.append((argv, fds, os.read(fds[0], 100), gpu.locked()))
        if len(fds) == 2:
            assert argv[-1] == f"/proc/self/fd/{fds[1]}"
            os.write(fds[1], b"whole")
            return _proc(0)
        return _proc(0, FMP4)

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=run)), _types_before_after(
        [WHOLE] * 4
    ), patch.object(seg_mod.os, "close", side_effect=lambda fd: closed.append(fd) or real_close(fd)):
        init, media = await fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW, gpu=gpu)
    assert init and media and not gpu.locked()
    (remux, (cut_fd, new_fd), data1, locked1), (transcode, fds2, data2, locked2) = calls
    assert "noise=drop=eq(n\\,3)+eq(n\\,5)" in remux and "copy" in remux
    assert data1 == b"cut" and not locked1  # re-muxed before taking the GPU
    assert fds2 == (new_fd,) and data2 == b"whole" and locked2 and "-hwaccel" in transcode
    assert sorted(closed) == sorted([cut_fd, new_fd])  # each once


@pytest.mark.parametrize("after", [None, [WHOLE] * 5, [WHOLE, WHOLE, WHOLE, HALF]])
async def test_a_remux_that_kept_other_frames_never_reaches_the_gpu(after) -> None:
    gpu = asyncio.Semaphore(1)
    closed = []
    real_close = os.close
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_proc(0))) as run, _types_before_after(
        after
    ), patch.object(seg_mod.os, "close", side_effect=lambda fd: closed.append(fd) or real_close(fd)):
        with pytest.raises(SSError, match="the 4 frames kept"):
            await fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW, gpu=gpu)
    assert run.await_count == 1 and len(closed) == 2 and not gpu.locked()


@pytest.mark.parametrize("spec", [SW, TranscodeSpec("hevc", True, False, 1280, 712)])
async def test_only_a_gpu_decoding_h265_is_checked(spec: TranscodeSpec) -> None:
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_proc(0, FMP4))) as run, patch.object(
        seg_mod, "slice_types"
    ) as check:
        await fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", spec)
    check.assert_not_called()
    assert run.await_count == 1


@pytest.mark.parametrize("cut", [_mp4([[19, 19]] * 2), b"not an mp4"])
async def test_whole_or_unreadable_cut_goes_to_the_gpu_as_it_is(cut: bytes) -> None:
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_proc(0, FMP4))) as run:
        await fetch_timelapse_segment(_dl_client(cut), _tseg(), "ffmpeg", HW)
    assert run.await_count == 1


async def test_dropping_frames_fails() -> None:
    gpu = asyncio.Semaphore(1)
    closed = []
    real_close = os.close
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_proc(1, b"", b"bad cut"))) as run, patch.object(
        seg_mod, "slice_types", return_value=CUT_TYPES
    ), patch.object(seg_mod.os, "close", side_effect=lambda fd: closed.append(fd) or real_close(fd)):
        with pytest.raises(SSError, match="bad cut"):
            await fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW, gpu=gpu)
    assert run.await_count == 1 and len(closed) == 2 and not gpu.locked()


async def test_dropping_frames_without_memfd() -> None:
    real = os.memfd_create
    made = []

    def once(*a):
        if made:
            raise OSError(24, "Too many open files")
        made.append(1)
        return real(*a)

    with patch.object(seg_mod.os, "memfd_create", side_effect=once), patch.object(
        seg_mod, "slice_types", return_value=CUT_TYPES
    ):
        with pytest.raises(SSError, match="OSError"):
            await fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW)


async def test_fetch_timelapse_segment_empty_output() -> None:
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_proc(0, b""))):
        with pytest.raises(SSError, match="empty output"):
            await fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", SW)


async def test_fetch_timelapse_segment_download_error_closes_the_file() -> None:
    client = MagicMock()
    client.download_to = AsyncMock(side_effect=SSError("x", "Download", 400))
    closed = []
    real_close = os.close
    with patch.object(seg_mod.os, "close", side_effect=lambda fd: closed.append(fd) or real_close(fd)):
        with pytest.raises(SSError):
            await fetch_timelapse_segment(client, _tseg(), "ffmpeg", HW)
    assert len(closed) == 1


async def test_fetch_timelapse_segment_without_memfd() -> None:
    with patch.object(seg_mod.os, "memfd_create", side_effect=OSError()):
        with pytest.raises(SSError, match="scratch file"):
            await fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW)


async def test_hardware_transcode_outlives_a_cancelled_caller() -> None:
    """A started QSV transcode is never killed: it finishes, and frees the GPU, on its own."""
    gate = asyncio.Event()
    proc = _proc(None)

    async def communicate():
        await gate.wait()
        proc.returncode = 0
        return FMP4, b""

    proc.communicate = AsyncMock(side_effect=communicate)
    gpu = asyncio.Semaphore(1)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        task = asyncio.create_task(fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW, gpu=gpu))
        while not gpu.locked():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gpu.locked()  # still transcoding
        gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
    assert not gpu.locked()
    proc.kill.assert_not_called()


async def test_software_transcode_is_killed_with_its_caller() -> None:
    proc = _proc(None)

    async def communicate():
        await asyncio.sleep(10)

    proc.communicate = AsyncMock(side_effect=communicate)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        task = asyncio.create_task(fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", SW))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    proc.kill.assert_called_once()


# ---- one transcode at a time, never killed once started ---------------------


async def test_transcodes_take_turns_on_the_gpu() -> None:
    gpu = asyncio.Semaphore(1)
    running = []
    peak = []

    async def run(*argv, **kwargs):
        proc = _proc(None)

        async def communicate():
            running.append(1)
            peak.append(len(running))
            await asyncio.sleep(0.01)
            running.pop()
            proc.returncode = 0
            return FMP4, b""

        proc.communicate = AsyncMock(side_effect=communicate)
        return proc

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=run)) as spawn:
        await asyncio.gather(*(fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW, gpu=gpu) for _ in range(3)))
    assert spawn.await_count == 3 and max(peak) == 1
    assert not gpu.locked()


async def test_waiting_for_the_gpu_is_cancellable_and_never_spawns() -> None:
    gpu = asyncio.Semaphore(1)
    await gpu.acquire()  # busy
    closed = []
    real_close = os.close
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn,
        patch.object(seg_mod.os, "close", side_effect=lambda fd: closed.append(fd) or real_close(fd)),
    ):
        task = asyncio.create_task(fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW, gpu=gpu))
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    spawn.assert_not_awaited()
    assert len(closed) == 1
    gpu.release()
    assert not gpu.locked()  # the cancelled waiter took nothing


async def test_hardware_without_a_lock_uses_the_module_one() -> None:
    seen = []

    async def run(*argv, **kwargs):
        seen.append(seg_mod._DEFAULT_GPU.locked())
        return _proc(0, FMP4)

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=run)):
        await fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW)
    assert seen == [True] and not seg_mod._DEFAULT_GPU.locked()


async def test_hardware_timeout_stops_with_sigterm_first(caplog: pytest.LogCaptureFixture) -> None:
    proc = _proc(None)

    async def communicate():
        await asyncio.sleep(10)

    async def wait():
        proc.returncode = -15
        return -15

    proc.communicate = AsyncMock(side_effect=communicate)
    proc.wait = AsyncMock(side_effect=wait)
    proc.terminate = MagicMock()
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        with pytest.raises(SSError, match="timed out"):
            await fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW, timeout=0.01, gpu=asyncio.Semaphore(1))
    proc.terminate.assert_called_once()
    proc.kill.assert_not_called()
    # Which cut failed is logged (its caller may be gone).
    assert f"rec={_tseg().recording_id} off={_tseg().offset_ms}ms" in caplog.text and "timed out" in caplog.text


async def test_sigterm_ignored_then_sigkill() -> None:
    proc = _proc(None)
    calls = []

    async def wait():
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(10)
        return -9

    proc.wait = AsyncMock(side_effect=wait)
    proc.terminate = MagicMock()
    with patch.object(seg_mod, "TERM_GRACE_SECONDS", 0.01):
        await seg_mod._stop(proc, gentle=True)
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


async def test_stop_a_process_already_gone() -> None:
    proc = _proc(None)
    proc.terminate = MagicMock(side_effect=ProcessLookupError())
    proc.kill = MagicMock(side_effect=ProcessLookupError())
    await seg_mod._stop(proc, gentle=True)
    proc.wait.assert_awaited()


async def test_drain_transcodes_waits_for_running_ones() -> None:
    gate = asyncio.Event()
    proc = _proc(None)

    async def communicate():
        await gate.wait()
        proc.returncode = 0
        return FMP4, b""

    proc.communicate = AsyncMock(side_effect=communicate)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        task = asyncio.create_task(fetch_timelapse_segment(_dl_client(), _tseg(), "ffmpeg", HW, gpu=asyncio.Semaphore(1)))
        while not seg_mod._JOBS:
            await asyncio.sleep(0)
        drain = asyncio.create_task(seg_mod.drain_transcodes(timeout=5))
        await asyncio.sleep(0.01)
        assert not drain.done()
        gate.set()
        await drain
        await task
    assert not seg_mod._JOBS
    await seg_mod.drain_transcodes()  # nothing running: returns at once


async def test_download_to_write_error_is_an_sserror(memfd: int) -> None:
    from synology_ss_playback import client as client_mod

    client, _ = _streaming_client([_stream([b"ab"])])
    with patch.object(client_mod, "_write_all", side_effect=OSError(12, "ENOMEM")):
        with pytest.raises(SSError, match="writing the cut"):
            await client.download_to(memfd, 3, 0, 0, 1000)
