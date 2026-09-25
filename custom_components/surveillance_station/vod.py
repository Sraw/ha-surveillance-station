"""Turn Surveillance Station recordings into an HLS VOD playlist.

Pure logic, no Home Assistant imports, so it can be unit tested on its own.

Surveillance Station stores continuous recordings as ~30 minute files and can
cut any time range out of one of them (``Recording.Download`` with
``offsetTimeMs``/``playTimeMs``). It returns a plain MP4 with the ``moov`` box
at the end and no Range support, so a browser cannot stream it directly. We
therefore plan fixed-length segments on the wall clock, fetch each one from
Surveillance Station on demand and remux it to fragmented MP4 (see
``views.py``). This module does the planning and the playlist text.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from datetime import datetime, timezone

SEGMENT_SECONDS = 10
# The recording that is still being written can be cut up to "now", but the
# last seconds are not flushed yet; stay this far behind real time.
LIVE_MARGIN_SECONDS = 5
# Anything shorter than this at the edge of a recording is not worth a segment.
MIN_SEGMENT_SECONDS = 1.0
# Two segments further apart than this are separated by a discontinuity.
GAP_TOLERANCE_SECONDS = 0.5


@dataclass(frozen=True)
class Recording:
    """One Surveillance Station recording file (wall-clock seconds)."""

    id: int
    start: float
    end: float
    mount_id: int = 0
    live: bool = False
    hevc: bool = True


@dataclass(frozen=True)
class Segment:
    """One HLS segment: a slice of a single recording."""

    index: int
    recording_id: int
    mount_id: int
    wall_start: float
    duration: float
    offset_ms: int  # position inside the recording file
    media_start: float  # position on the playlist timeline
    discontinuity: bool  # a gap in recordings precedes this segment
    new_map: bool  # first segment of a recording -> own init segment
    hevc: bool = True


@dataclass(frozen=True)
class Run:
    """A stretch of the playlist that maps linearly to wall-clock time."""

    wall_start: float
    media_start: float
    duration: float


def plan_segments(
    recordings: list[Recording],
    start: float,
    end: float,
    now: float,
    segment_seconds: int = SEGMENT_SECONDS,
) -> list[Segment]:
    """Cover [start, end) with segments cut from the given recordings.

    Segment boundaries sit on a fixed wall-clock grid (multiples of
    ``segment_seconds``) so that the same moment always maps to the same
    segment, which keeps caching simple and seeking predictable. Boundaries
    are additionally split at recording edges, because one segment can only
    come from one recording file.
    """
    segments: list[Segment] = []
    media_pos = 0.0
    prev_end: float | None = None
    prev_rec: int | None = None
    for rec in sorted(recordings, key=lambda r: r.start):
        rec_end = min(rec.end, now - LIVE_MARGIN_SECONDS) if rec.live else rec.end
        lo = max(start, rec.start)
        hi = min(end, rec_end)
        t = lo
        while hi - t >= MIN_SEGMENT_SECONDS:
            grid_next = (math.floor(t / segment_seconds) + 1) * segment_seconds
            seg_end = min(grid_next, hi)
            if seg_end - t < MIN_SEGMENT_SECONDS and seg_end < hi:
                # A sliver before the next grid line: fold it into the next one.
                seg_end = min(grid_next + segment_seconds, hi)
            duration = seg_end - t
            if duration < MIN_SEGMENT_SECONDS:
                break
            gap = prev_end is not None and abs(t - prev_end) > GAP_TOLERANCE_SECONDS
            segments.append(
                Segment(
                    index=len(segments),
                    recording_id=rec.id,
                    mount_id=rec.mount_id,
                    wall_start=t,
                    duration=duration,
                    offset_ms=int(round((t - rec.start) * 1000)),
                    media_start=media_pos,
                    discontinuity=gap,
                    new_map=rec.id != prev_rec or gap,
                    hevc=rec.hevc,
                )
            )
            media_pos += duration
            prev_end = seg_end
            prev_rec = rec.id
            t = seg_end
    return segments


def runs_from_segments(segments: list[Segment]) -> list[Run]:
    """Collapse contiguous segments into wall-clock <-> media-time runs."""
    runs: list[Run] = []
    for seg in segments:
        if runs and not seg.discontinuity:
            last = runs[-1]
            runs[-1] = Run(last.wall_start, last.media_start, last.duration + seg.duration)
        else:
            runs.append(Run(seg.wall_start, seg.media_start, seg.duration))
    return runs


def _pdt(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def render_playlist(segments: list[Segment]) -> str:
    """Render an HLS v7 VOD media playlist with fMP4 segments.

    URIs are relative to the playlist: ``init/<index>.mp4`` (the init
    segment derived from segment <index>) and ``seg/<index>.m4s``.
    """
    target = max((math.ceil(s.duration) for s in segments), default=SEGMENT_SECONDS)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for seg in segments:
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        if seg.new_map:
            lines.append(f'#EXT-X-MAP:URI="init/{seg.index}.mp4"')
        if seg.index == 0 or seg.discontinuity:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_pdt(seg.wall_start)}")
        lines.append(f"#EXTINF:{seg.duration:.3f},")
        lines.append(f"seg/{seg.index}.m4s")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def iter_boxes(data: bytes):
    """Yield (type, bytes) for each top-level ISO BMFF box."""
    i = 0
    n = len(data)
    while i + 8 <= n:
        size, typ = struct.unpack(">I4s", data[i : i + 8])
        if size == 1:
            if i + 16 > n:
                break
            size = struct.unpack(">Q", data[i + 8 : i + 16])[0]
        elif size == 0:
            size = n - i
        if size < 8 or i + size > n:
            break
        yield typ.decode("latin-1"), data[i : i + size]
        i += size


def split_fmp4(data: bytes) -> tuple[bytes, bytes]:
    """Split ffmpeg's fragmented MP4 output into (init, media) parts."""
    init = bytearray()
    media = bytearray()
    for typ, box in iter_boxes(data):
        if typ in ("ftyp", "moov"):
            init += box
        elif typ in ("styp", "moof", "mdat", "sidx", "prft"):
            media += box
    return bytes(init), bytes(media)


def ffmpeg_remux_args(
    ffmpeg: str, src: str, duration: float, media_start: float, hevc: bool = True
) -> list[str]:
    """ffmpeg command: stream-copy one cut into fragmented MP4 on stdout.

    * ``-t`` trims Surveillance Station's keyframe-rounded cut (it returns
      ~1-2 s more than asked) back to the planned duration.
    * ``-output_ts_offset`` places the fragment at its playlist position so
      consecutive segments have continuous timestamps. It only reaches the
      fragments' ``tfdt`` with ``delay_moov+frag_discont``: under
      ``empty_moov`` (or without ``frag_discont``) ffmpeg rebases every
      output to tfdt=0 and parks the offset in an edit list, which MSE
      players ignore - every segment would then play at position 0.
    * ``hvc1`` tagging is what Safari/iOS require for HEVC; others accept it.
    """
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        src,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-t",
        f"{duration:.3f}",
        "-c",
        "copy",
        *(["-tag:v", "hvc1"] if hevc else []),
        "-video_track_timescale",
        "90000",
        "-output_ts_offset",
        f"{media_start:.3f}",
        "-movflags",
        "+frag_keyframe+delay_moov+default_base_moof+frag_discont+skip_trailer",
        "-f",
        "mp4",
        "pipe:1",
    ]
    return args
