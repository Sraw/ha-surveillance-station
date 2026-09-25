"""Fetch one planned segment: cut it on the NAS, remux it to fragmented MP4.

``Recording.Download`` returns a plain MP4 with the ``moov`` box at the end
and no Range support, so ffmpeg needs it as a seekable file. The scratch file
lives only for the remux; a crash can leave one behind, which
``remove_stale_temp_files`` sweeps up at startup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import glob
import logging
import os
import tempfile
import time

from .client import RecordingInfo, SSError, SurveillanceStationClient
from .vod import Recording, Segment, ffmpeg_remux_args, split_fmp4

_LOGGER = logging.getLogger(__name__)

TEMP_PREFIX = "ss_vod_"
REMUX_TIMEOUT_SECONDS = 30


def recordings_from(infos: Iterable[RecordingInfo]) -> list[Recording]:
    """What the planner needs from the client's recording list."""
    return [Recording(r.id, r.start, r.end, r.mount_id, r.live, r.hevc) for r in infos]


async def fetch_segment(
    client: SurveillanceStationClient,
    seg: Segment,
    ffmpeg: str,
    timeout: float = REMUX_TIMEOUT_SECONDS,
) -> tuple[bytes, bytes]:
    """Return (init, media) fragmented-MP4 parts of one segment."""
    started = time.monotonic()
    # Ask for a little extra; SS rounds to keyframes and ffmpeg -t trims.
    raw = await client.download(
        seg.recording_id, seg.mount_id, seg.offset_ms, int(seg.duration * 1000) + 1000
    )
    fetched = time.monotonic()
    data = await _remux(ffmpeg, raw, seg, timeout)
    init, media = split_fmp4(data)
    if not init or not media:
        raise SSError("remux", "split", None, f"empty output for segment {seg.index}")
    _LOGGER.debug(
        "segment %s rec=%s off=%sms dur=%.1fs: download %.2fs (%d KB), remux %.2fs",
        seg.index, seg.recording_id, seg.offset_ms, seg.duration,
        fetched - started, len(raw) // 1024, time.monotonic() - fetched,
    )
    return init, media


async def _remux(ffmpeg: str, raw: bytes, seg: Segment, timeout: float) -> bytes:
    fd, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp4")
    proc = None
    try:
        await asyncio.to_thread(_write_and_close, fd, raw)
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_remux_args(ffmpeg, path, seg.duration, seg.media_start, seg.hevc),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
        except TimeoutError:
            raise SSError("ffmpeg", "remux", None, "timed out") from None
        if proc.returncode != 0:
            raise SSError("ffmpeg", "remux", proc.returncode, err.decode(errors="replace")[-400:])
        return out
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        await asyncio.to_thread(_unlink, path)


def remove_stale_temp_files() -> int:
    """Delete scratch files a crash (or a killed process) left behind.

    Blocking; call it from an executor, and only at startup, when no remux
    can be running.
    """
    removed = 0
    for path in glob.glob(os.path.join(tempfile.gettempdir(), f"{TEMP_PREFIX}*.mp4")):
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def _write_and_close(fd: int, data: bytes) -> None:
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
