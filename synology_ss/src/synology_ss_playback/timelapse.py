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
from dataclasses import dataclass
import math

from .client import TimelapseRecording
from .vod import Segment

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
    """Segments (and the media -> wall map) covering [day_start, day_end).

    Pass one camera's files. Segment boundaries are whole video seconds on a
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
        v0 = max(0, round((lo - rec.start) / rate))
        if prev_end is not None and prev_end > rec.start + v0 * rate:
            # Overlapping the last file: start at or after where it ended,
            # never a frame it already played.
            v0 = max(v0, math.ceil((prev_end - rec.start) / rate - 1e-9))
        v1 = min(math.floor(usable), round((hi - rec.start) / rate))
        if v1 - v0 < 1:
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
        head = ["-i", src]
        video = [
            "-vf", f"scale={w}:{h}", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(SW_CRF),
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
        proc.kill()
        await proc.wait()
        return None
