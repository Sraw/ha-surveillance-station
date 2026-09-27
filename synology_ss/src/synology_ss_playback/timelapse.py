"""Play a day of a Surveillance Station time-lapse task as HLS.

Pure planning plus the ffmpeg command lines; the I/O is in ``segment.py``.

A time-lapse file is a video of up to 6 minutes at TIMELAPSE_FPS covering up
to a day of wall time, starting whenever the task rolled over (not at
midnight). SS stores it as all-intra 4512x2512 H.265 at ~90-240 Mbps, far
too much for a browser, so segments are transcoded (see
``ffmpeg_transcode_args``), not stream-copied like recordings.

A day is played as the stretches of the files that fall on it. Wall time is
mapped to video time linearly per file: SS's own player does the same (its
seekbar runs from ``startTime`` to ``startTime + rangeMinute``). Stretches SS
slowed down for its events make that a little off - they take 4x the video
time - but the files measured here were within a minute over a day, and
boundaries are whole video seconds (4 minutes of wall time at 240x) anyway.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
import logging
import math
import os

from .client import TimelapseRecording
from .vod import Segment

_LOGGER = logging.getLogger(__name__)

# SS writes time-lapse video at this rate (every task on SS 9.x).
TIMELAPSE_FPS = 30
# Seconds of video per segment. A cut of N s comes back with 29 frames more
# (whatever the milliseconds past N), and a second of daytime 4K is ~30 MB at
# the gigabit link's ~100 MB/s: 4 s takes ~1.5 s to fetch - the wait on a
# seek - and wastes a fifth of it; 6 s would take ~2 s.
TIMELAPSE_SEGMENT_SECONDS = 4
# A file still being written is cut this far (video seconds) behind its end.
TIMELAPSE_LIVE_MARGIN_SECONDS = 2
OUTPUT_WIDTH = 1280
# QSV's ICQ quality (lower is better): ~1.7 Mbps H.265 / 2.2 Mbps H.264 at 1280.
HW_QUALITY = 28
SW_CRF = 26
# A software transcode uses at most half the CPUs (decoding, scaling and
# encoding each), leaving the rest to Home Assistant and whatever else runs.
SW_THREADS = max(1, (getattr(os, "process_cpu_count", os.cpu_count)() or 2) // 2)
# A GPU check that timed out is stopped with SIGTERM, then SIGKILL after this.
CHECK_TERM_GRACE_SECONDS = 5
CODECS = ("hevc", "h264")


@dataclass(frozen=True)
class TimelapseRun:
    """A stretch of the playlist cut from one file: media time -> wall time."""

    media_start: float
    duration: float
    wall_start: float
    rate: float  # wall seconds per second of video
    recording_id: int


@dataclass(frozen=True)
class TranscodeSpec:
    """How a segment is transcoded."""

    codec: str  # "hevc" or "h264" (the output)
    hardware: bool  # Intel QSV; software is H.264 only
    source_hevc: bool
    width: int  # of the output
    height: int


def video_seconds(rec: TimelapseRecording) -> float:
    return rec.frames / TIMELAPSE_FPS


def covered(rec: TimelapseRecording) -> tuple[float, float]:
    """The wall-time stretch a file can be played for (plan_day's view of it)."""
    full = video_seconds(rec)
    if rec.span <= 0 or full <= 0:
        return rec.start, rec.start
    usable = full - TIMELAPSE_LIVE_MARGIN_SECONDS if rec.live else full
    return rec.start, rec.start + max(0.0, usable) * rec.span / full


def plan_day(
    recordings: list[TimelapseRecording],
    day_start: float,
    day_end: float,
    segment_seconds: int = TIMELAPSE_SEGMENT_SECONDS,
) -> tuple[list[Segment], list[TimelapseRun]]:
    """Segments (and the media -> wall map) for the day [day_start, day_end).

    Pass one camera's files. A file is played from its first whole video
    second at or after day_start up to its first at or after day_end (a
    second is minutes of wall time), so the next day picks up exactly where
    this one stopped. Segment boundaries are whole video seconds on a
    grid from each file's start, so the same stretch is always the same cut.
    Where files overlap, the later one starts where the earlier one ended.
    The playlist has no discontinuities: gaps between files are skipped, and
    every segment is transcoded to the same format.
    """
    segments: list[Segment] = []
    runs: list[TimelapseRun] = []
    media = 0.0
    prev_end: float | None = None
    for rec in sorted(recordings, key=lambda r: r.start):
        full = video_seconds(rec)
        if rec.span <= 0 or full <= 0:
            continue
        rate = rec.span / full
        usable = full - TIMELAPSE_LIVE_MARGIN_SECONDS if rec.live else full
        lo = max(day_start, rec.start)
        hi = min(day_end, rec.start + usable * rate)
        # Whole video seconds, rounded up at both ends: a day never starts
        # before its midnight (a second is ~4 minutes of wall time), and one
        # day ends on the very second the next one starts.
        v0 = max(0, math.ceil((lo - rec.start) / rate - 1e-9))
        if prev_end is not None and prev_end > rec.start + v0 * rate:
            # Overlapping the last file: start at or after where it ended,
            # never a frame it already played.
            v0 = math.ceil((prev_end - rec.start) / rate - 1e-9)
        v1 = min(math.floor(usable), math.ceil((hi - rec.start) / rate - 1e-9))
        if v1 - v0 < 1:
            if lo <= hi and v0 <= math.floor(usable):
                # The day before played this file up to here: a file
                # overlapping it must not start any earlier. (Also within a
                # day, where it can leave up to a video second of the next
                # file unplayed: the price of both days' plans agreeing.)
                prev_end = max(prev_end if prev_end is not None else -math.inf, rec.start + v0 * rate)
            continue
        runs.append(TimelapseRun(media, v1 - v0, rec.start + v0 * rate, rate, rec.id))
        v = v0
        while v < v1:
            nxt = min((v // segment_seconds + 1) * segment_seconds, v1)
            segments.append(
                Segment(
                    index=len(segments),
                    recording_id=rec.id,
                    mount_id=0,
                    wall_start=rec.start + v * rate,
                    duration=float(nxt - v),
                    offset_ms=v * 1000,
                    media_start=media + (v - v0),
                    discontinuity=False,
                    new_map=v == v0,
                    hevc=rec.hevc,
                )
            )
            v = nxt
        media += v1 - v0
        prev_end = rec.start + v1 * rate
    return segments, runs


# IRAP pictures: BLA, IDR, CRA (all-intra streams are made of these).
_IRAP = range(16, 22)


def slice_types(read: Callable[[int, int], bytes], size: int) -> list[tuple[int, ...]] | None:
    """The NAL unit types of the slices of each sample of an MP4's H.265 track.

    ``read(offset, n)`` reads the file (``size`` bytes); only the NAL unit
    headers are read. None if it has no H.265 video track or can't be parsed.
    """
    try:
        return _slice_types(read, size)
    except Exception as err:  # noqa: BLE001 - a file it can't read isn't checked
        _LOGGER.debug("can't read the slices of an MP4: %s: %s", type(err).__name__, err)
        return None


def broken_frames(frames: list[tuple[int, ...]]) -> list[int]:
    """Indices of the frames (from ``slice_types``) that aren't whole pictures.

    A Reolink E1 Outdoor Pro (firmware v3.1.0.5714, on Wi-Fi) now and then
    leaves a time-lapse frame with one of its two slices - one per tile
    column - missing, or with the second one taken from another picture (a
    P slice in an all-intra stream). The CPU decoder conceals both; an Iris
    Xe decodes such 4K frames with both VDBoxes, one tile column each, and
    hangs on the first kind (reproduced: every such cut, in any driver and
    ffmpeg version; none once the frame was dropped) and crashes ffmpeg on
    the second.

    Broken: fewer slices than most of the frames have, or - in a stream of
    mostly all-intra frames - a slice that isn't intra, or slices of
    different types. They are simply dropped: a 30th of a second of video.
    """
    if not frames:
        return []
    usual = Counter(len(f) for f in frames).most_common(1)[0][0]
    intra = sum(1 for f in frames if f and all(t in _IRAP for t in f)) * 2 > len(frames)
    return [
        i for i, f in enumerate(frames)
        if len(f) < usual or (intra and (not f or len(set(f)) > 1 or f[0] not in _IRAP))
    ]


def _boxes(read: Callable[[int, int], bytes], start: int, end: int):
    """(type, payload start, payload end) of the boxes in [start, end)."""
    pos = start
    while pos + 8 <= end:
        head = read(pos, 16)
        size, kind = int.from_bytes(head[:4], "big"), head[4:8].decode("latin-1")
        hdr = 8
        if size == 1:
            size, hdr = int.from_bytes(head[8:16], "big"), 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise ValueError("bad box")
        yield kind, pos + hdr, pos + size
        pos += size


def _child(read, box: tuple[int, int], path: str) -> tuple[int, int]:
    for name in path.split("/"):
        box = next(((a, b) for kind, a, b in _boxes(read, *box) if kind == name), None)
        if box is None:
            raise ValueError(f"no {name}")
    return box


def _table(read, box: tuple[int, int], head: int, count: int, width: int) -> list[int]:
    """``count`` big-endian integers of ``width`` bytes, ``head`` bytes into a box."""
    if head + count * width > box[1] - box[0]:
        raise ValueError("table past its box")
    raw = read(box[0] + head, count * width)
    return [int.from_bytes(raw[i : i + width], "big") for i in range(0, count * width, width)]


def _slice_types(read: Callable[[int, int], bytes], size: int) -> list[tuple[int, ...]] | None:
    moov = _child(read, (0, size), "moov")
    for kind, a, b in _boxes(read, *moov):
        if kind != "trak":
            continue
        hdlr = _child(read, (a, b), "mdia/hdlr")
        if read(hdlr[0] + 8, 4) == b"vide":
            stbl = _child(read, (a, b), "mdia/minf/stbl")
            break
    else:
        return None
    # The sample entry (hvc1/hev1): 78 bytes of VisualSampleEntry, then hvcC.
    stsd = _child(read, stbl, "stsd")
    kind, ea, eb = next(_boxes(read, stsd[0] + 8, stsd[1]), ("", 0, 0))
    if kind not in ("hvc1", "hev1"):
        return None
    hvcc = _child(read, (ea + 78, eb), "hvcC")
    length = (read(hvcc[0] + 21, 1)[0] & 3) + 1
    stsz = _child(read, stbl, "stsz")
    fixed, count = int.from_bytes(read(stsz[0] + 4, 4), "big"), int.from_bytes(read(stsz[0] + 8, 4), "big")
    if fixed:
        if fixed * count > size:
            raise ValueError("samples past the file")
        sizes = [fixed] * count
    else:
        sizes = _table(read, stsz, 12, count, 4)
    stsc = _child(read, stbl, "stsc")
    table = _table(read, stsc, 8, int.from_bytes(read(stsc[0] + 4, 4), "big") * 3, 4)
    runs = list(zip(table[0::3], table[1::3]))  # (first chunk, samples per chunk)
    try:
        co, width = _child(read, stbl, "stco"), 4
    except ValueError:
        co, width = _child(read, stbl, "co64"), 8
    offsets = _table(read, co, 8, int.from_bytes(read(co[0] + 4, 4), "big"), width)
    slices = []
    sample = 0
    for c, chunk_offset in enumerate(offsets, start=1):
        per_chunk = next((spc for first, spc in reversed(runs) if first <= c), None)
        if per_chunk is None:
            raise ValueError("chunk before the first run")
        pos = chunk_offset
        for _ in range(per_chunk):
            if sample >= count:
                break
            end, types = pos + sizes[sample], []
            if end > size:
                raise ValueError("sample past the file")
            while pos + length < end:
                head = read(pos, length + 1)
                nal = int.from_bytes(head[:length], "big")
                if (kind := (head[length] >> 1) & 0x3F) < 32:  # VCL: a slice segment
                    types.append(kind)
                pos += length + nal
            if pos != end:  # its NAL units don't add up to it: a broken frame
                types, pos = [], end
            slices.append(tuple(types))
            sample += 1
    if sample != count:
        raise ValueError("samples without a chunk")
    return slices


def output_size(width: int, height: int, target: int = OUTPUT_WIDTH) -> tuple[int, int]:
    """Output dimensions: target wide (never upscaled), even, same aspect."""
    if width <= 0 or height <= 0:
        width, height = 16, 9
        w = target
    else:
        w = min(target, width)
    w -= w % 2
    h = max(2, round(w * height / width / 2) * 2)
    return w, h


def ffmpeg_transcode_args(
    ffmpeg: str, src: str, duration: float, media_start: float, spec: TranscodeSpec
) -> list[str]:
    """ffmpeg command: transcode one cut into fragmented MP4 on stdout.

    Timestamps as in ``vod.ffmpeg_remux_args`` (``-output_ts_offset`` with
    ``delay_moov+frag_discont``); one keyframe (and fragment) per second of
    output, so a player can start anywhere.
    """
    w, h = spec.width, spec.height
    if spec.hardware:
        decoder = "hevc_qsv" if spec.source_hevc else "h264_qsv"
        encoder = "hevc_qsv" if spec.codec == "hevc" else "h264_qsv"
        head = ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", decoder, "-i", src]
        video = [
            "-vf", f"scale_qsv=w={w}:h={h}",
            "-c:v", encoder, "-preset", "veryfast", "-global_quality", str(HW_QUALITY),
        ]
    else:
        threads = str(SW_THREADS)
        head = ["-filter_threads", threads, "-threads", threads, "-i", src]
        video = [
            "-vf", f"scale={w}:{h}", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(SW_CRF), "-threads", threads,
        ]
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        *head,
        "-map", "0:v:0", "-an",
        "-t", f"{duration:.3f}",
        *video,
        "-g", str(TIMELAPSE_FPS),
        *(["-tag:v", "hvc1"] if spec.hardware and spec.codec == "hevc" else []),
        "-video_track_timescale", "90000",
        "-output_ts_offset", f"{media_start:.3f}",
        "-movflags", "+frag_keyframe+delay_moov+default_base_moof+frag_discont+skip_trailer",
        "-f", "mp4", "pipe:1",
    ]


async def hardware_transcode_available(ffmpeg: str, timeout: float = 20) -> bool | None:
    """Whether this ffmpeg can encode H.265 on an Intel GPU (QSV) here.

    None: couldn't tell (the check timed out, e.g. while another process
    was loading the GPU); worth asking again later.
    """
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-init_hw_device", "qsv=hw", "-filter_hw_device", "hw",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=0.5",
        "-vf", "hwupload=extra_hw_frames=16,format=qsv",
        "-c:v", "hevc_qsv", "-f", "null", "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return False
    try:
        async with asyncio.timeout(timeout):
            return await proc.wait() == 0
    except TimeoutError:
        # SIGTERM first: SIGKILLing QSV sessions mid-way hung an iGPU (see
        # segment.fetch_timelapse_segment), and a check times out when it's busy.
        proc.terminate()
        try:
            async with asyncio.timeout(CHECK_TERM_GRACE_SECONDS):
                await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
        return None
