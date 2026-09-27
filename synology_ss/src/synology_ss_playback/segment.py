"""Fetch one planned segment (cut on the NAS, remuxed to fragmented MP4; a
time-lapse one transcoded), or a single frame of a recording as a JPEG.

``Recording.Download`` returns a plain MP4 with the ``moov`` box at the end
and no Range support, so ffmpeg needs it as a seekable file. The scratch file
lives only for the remux; a crash can leave one behind, which
``remove_stale_temp_files`` sweeps up at startup.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Iterable
import glob
import logging
import os
import re
import shutil
import tempfile
import time

from .client import RecordingInfo, SSError, SurveillanceStationClient
from .timelapse import TranscodeSpec, broken_frames, ffmpeg_transcode_args, slice_types
from .vod import Recording, Segment, ffmpeg_remux_args, split_fmp4

_LOGGER = logging.getLogger(__name__)

TEMP_PREFIX = "ss_vod_"
REMUX_TIMEOUT_SECONDS = 30
# A process asked to stop with SIGTERM gets this long before SIGKILL.
TERM_GRACE_SECONDS = 5
# Every ffmpeg runs this much below Home Assistant's own priority (through
# nice(1), so its threads have it from the start), where there is a nice.
FFMPEG_NICE = 10
_NICE = shutil.which("nice")
SNAPSHOT_WIDTH = 320


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
    data = await _remux(ffmpeg, raw, seg, timeout, client.nas)
    init, media = split_fmp4(data)
    if not init or not media:
        raise SSError("remux", "split", None, f"empty output for segment {seg.index}")
    _LOGGER.debug(
        "segment %s rec=%s off=%sms dur=%.1fs: download %.2fs (%d KB), remux %.2fs",
        seg.index, seg.recording_id, seg.offset_ms, seg.duration,
        fetched - started, len(raw) // 1024, time.monotonic() - fetched,
    )
    return init, media


# Recordings (of which NAS, of which id) whose audio MP4 can't carry: their
# later segments go without it straight away, and consistently (one init).
_NO_AUDIO: OrderedDict[tuple[str, int], None] = OrderedDict()
# ffmpeg's own words for it; not damaged input ("Could not find codec parameters").
_AUDIO_REFUSED = re.compile(r"Could not find tag for codec|not currently supported in container", re.IGNORECASE)


async def _remux(ffmpeg: str, raw: bytes, seg: Segment, timeout: float, nas: str = "") -> bytes:
    if (nas, seg.recording_id) not in _NO_AUDIO:
        try:
            return await _run_ffmpeg(
                ffmpeg, raw, lambda src: ffmpeg_remux_args(ffmpeg, src, seg.duration, seg.media_start, seg.hevc), timeout
            )
        except SSError as err:
            # An audio codec MP4 can't carry (G.711, G.726: many cameras'
            # default): the video without it rather than nothing.
            if err.method != "run" or err.code is None or not _AUDIO_REFUSED.search(str(err)):
                raise
        _LOGGER.debug("Recording %s: audio MP4 can't carry; its segments go without", seg.recording_id)
        _NO_AUDIO[(nas, seg.recording_id)] = None
        while len(_NO_AUDIO) > 256:
            _NO_AUDIO.popitem(last=False)
    return await _run_ffmpeg(
        ffmpeg, raw,
        lambda src: ffmpeg_remux_args(ffmpeg, src, seg.duration, seg.media_start, seg.hevc, audio=False),
        timeout,
    )


async def _run_ffmpeg(
    ffmpeg: str, raw: bytes, args: Callable[[str], list[str]], timeout: float
) -> bytes:
    """Run ffmpeg on a downloaded cut (via a scratch file; see module docstring)."""
    # A full /tmp or a missing ffmpeg is an SSError like any other failure
    # here, so callers answer 502 and keep going rather than crash.
    try:
        fd, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp4")
    except OSError as err:
        raise SSError("ffmpeg", "scratch file", None, type(err).__name__) from None
    try:
        try:
            await asyncio.to_thread(_write_and_close, fd, raw)
        except OSError as err:
            raise SSError("ffmpeg", "start", None, f"{type(err).__name__}: {err.strerror}") from None
        return await _exec_ffmpeg(args(path), timeout)
    finally:
        await asyncio.to_thread(_unlink, path)


async def _exec_ffmpeg(
    argv: list[str], timeout: float, pass_fds: tuple[int, ...] = (), gentle: bool = False
) -> bytes:
    """Run ffmpeg to completion and return its stdout; any failure is an SSError.

    ``gentle``: stop it (timeout, cancellation) with SIGTERM first, which
    lets ffmpeg close its codecs, and SIGKILL only if it hasn't gone in
    TERM_GRACE_SECONDS (see fetch_timelapse_segment for why).
    """
    if _NICE:
        argv = [_NICE, "-n", str(FFMPEG_NICE), *argv]  # nice execs ffmpeg: same process
    proc = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=pass_fds,
            )
        except OSError as err:
            raise SSError("ffmpeg", "start", None, f"{type(err).__name__}: {err.strerror}") from None
        try:
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
        except TimeoutError:
            raise SSError("ffmpeg", "run", None, "timed out") from None
        if proc.returncode != 0:
            raise SSError("ffmpeg", "run", proc.returncode, err.decode(errors="replace")[-400:])
        return out
    finally:
        if proc is not None and proc.returncode is None:
            await _stop(proc, gentle)


async def _stop(proc: asyncio.subprocess.Process, gentle: bool) -> None:
    if gentle:
        try:
            proc.terminate()
            async with asyncio.timeout(TERM_GRACE_SECONDS):
                await proc.wait()
            return
        except (TimeoutError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


# Hardware transcodes that are running (whoever asked for them), for
# drain_transcodes; and the lock hardware transcodes take when the caller
# brings none.
_JOBS: set[asyncio.Task] = set()
_DEFAULT_GPU = asyncio.Semaphore(1)


async def fetch_timelapse_segment(
    client: SurveillanceStationClient,
    seg: Segment,
    ffmpeg: str,
    spec: TranscodeSpec,
    timeout: float = REMUX_TIMEOUT_SECONDS,
    gpu: asyncio.Semaphore | None = None,
) -> tuple[bytes, bytes]:
    """Return (init, media) of one time-lapse segment, transcoded per ``spec``.

    The cut (up to ~200 MB of daytime 4K) goes into an anonymous in-memory
    file rather than a scratch file on disk: an hour of viewing would
    otherwise write ~100 GB. ffmpeg reads it through /proc/self/fd, which is
    seekable (the moov box is at the end).

    Transcodes run one at a time under ``gpu`` (a hardware one under a
    module-wide lock if none is given). A hardware transcode, once started,
    is never killed by its caller going away: it finishes (under a second)
    on its own, then frees the lock. SIGKILLing hevc_qsv decodes mid-way
    while others run hung an Intel iGPU (i915 "GPU HANG", reproduced), and
    the reset stalls everything else on it. Waiting for the lock is
    cancellable: a cut nobody wants any more never reaches the GPU. On
    shutdown, drain_transcodes() lets the running one finish; the timeout
    stops one with SIGTERM before SIGKILL.

    An H.265 cut goes to the GPU without its broken frames (see
    timelapse.broken_frames): one of those hangs it.
    """
    if gpu is None and spec.hardware:
        gpu = _DEFAULT_GPU
    started = time.monotonic()
    try:
        fd = os.memfd_create("ss_timelapse", os.MFD_CLOEXEC)
    except (AttributeError, OSError) as err:
        raise SSError("ffmpeg", "scratch file", None, type(err).__name__) from None
    cut = _Cut(fd)
    try:
        size = await client.download_to(
            fd, seg.recording_id, seg.mount_id, seg.offset_ms, int(seg.duration * 1000), timelapse=True
        )
        fetched = time.monotonic()
        if spec.hardware and spec.source_hevc:
            await _drop_broken_frames(ffmpeg, cut, seg, timeout)
        if gpu is not None:
            await gpu.acquire()
            cut.lock = gpu
    except BaseException:
        cut.close()
        raise
    argv = ffmpeg_transcode_args(ffmpeg, f"/proc/self/fd/{cut.fd}", seg.duration, seg.media_start, spec)
    job = asyncio.ensure_future(_transcode(argv, cut, timeout, spec.hardware))
    # Whatever happens to the job - even cancelled before it ran (loop
    # teardown) - the file is closed and the lock freed, exactly once. A
    # failure is logged with its cut: the caller may be gone (shielded).
    def done(t: asyncio.Future) -> None:
        cut.close()
        if not t.cancelled() and (err := t.exception()) is not None:
            _LOGGER.warning(
                "time-lapse transcode rec=%s off=%sms dur=%.0fs failed: %s",
                seg.recording_id, seg.offset_ms, seg.duration, str(err)[-300:],
            )

    job.add_done_callback(done)
    if spec.hardware:
        _JOBS.add(job)
        job.add_done_callback(_JOBS.discard)
        data = await asyncio.shield(job)
    else:
        data = await job
    init, media = split_fmp4(data)
    if not init or not media:
        raise SSError("transcode", "split", None, f"empty output for segment {seg.index}")
    _LOGGER.debug(
        "time-lapse segment %s rec=%s off=%sms dur=%.0fs: download %.2fs (%d MB), transcode %.2fs (%d KB, %s%s)",
        seg.index, seg.recording_id, seg.offset_ms, seg.duration, fetched - started, size >> 20,
        time.monotonic() - fetched, len(media) >> 10, spec.codec, " qsv" if spec.hardware else "",
    )
    return init, media


class _Cut:
    """A downloaded cut's file, and the transcode lock taken for it: released once."""

    def __init__(self, fd: int) -> None:
        self.fd: int | None = fd
        self.lock: asyncio.Semaphore | None = None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.lock is not None:
            self.lock.release()
            self.lock = None


async def _drop_broken_frames(ffmpeg: str, cut: _Cut, seg: Segment, timeout: float) -> None:
    """Re-mux the cut without its broken frames (timelapse.broken_frames), if any.

    The frame before stays on screen for a dropped one (a 30th of a second of
    video) and the rest keep their timestamps; a dropped first or last frame
    leaves the segment that much short. The re-muxed cut must hold exactly
    the frames that were kept - otherwise it doesn't go to the GPU. Holds the
    cut twice in memory meanwhile.
    """
    def reader(fd: int) -> Callable[[int, int], bytes]:
        return lambda offset, n: os.pread(fd, n, offset)

    frames = slice_types(reader(cut.fd), os.fstat(cut.fd).st_size)
    drop = broken_frames(frames or [])
    if not drop:
        return
    _LOGGER.info(
        "time-lapse rec=%s off=%sms: dropping %d broken frame(s) (%s) before the GPU decodes it",
        seg.recording_id, seg.offset_ms, len(drop), ", ".join(map(str, drop)),
    )
    try:
        out = os.memfd_create("ss_timelapse", os.MFD_CLOEXEC)
    except OSError as err:
        raise SSError("ffmpeg", "scratch file", None, type(err).__name__) from None
    expr = "+".join(f"eq(n\\,{i})" for i in drop)
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", f"/proc/self/fd/{cut.fd}",
        "-map", "0:v:0", "-c", "copy", "-bsf:v", f"noise=drop={expr}",
        "-f", "mp4", "-y", f"/proc/self/fd/{out}",
    ]
    try:
        await _exec_ffmpeg(argv, timeout, pass_fds=(cut.fd, out))
    except BaseException:
        os.close(out)
        raise
    old, cut.fd = cut.fd, out
    os.close(old)
    dropped = set(drop)
    kept = [f for i, f in enumerate(frames) if i not in dropped]
    if slice_types(reader(out), os.fstat(out).st_size) != kept:
        raise SSError("ffmpeg", "drop frames", None, f"the re-muxed cut isn't the {len(kept)} frames kept")


async def _transcode(argv: list[str], cut: _Cut, timeout: float, hardware: bool) -> bytes:
    """Run the transcode on the cut (then closed, and its lock freed)."""
    try:
        return await _exec_ffmpeg(argv, timeout, pass_fds=(cut.fd,), gentle=hardware)
    finally:
        cut.close()


async def drain_transcodes(timeout: float = REMUX_TIMEOUT_SECONDS + TERM_GRACE_SECONDS) -> None:
    """Wait (at most timeout) for the hardware transcodes running to finish.

    Call it before the event loop goes away: its teardown would kill them.
    """
    if _JOBS:
        await asyncio.wait(list(_JOBS), timeout=timeout)


async def fetch_snapshot(
    client: SurveillanceStationClient,
    camera_id: int,
    t: float,
    ffmpeg: str,
    width: int = SNAPSHOT_WIDTH,
    timeout: float = REMUX_TIMEOUT_SECONDS,
) -> bytes | None:
    """A JPEG of what the camera recorded at wall time t, or None if nothing was.

    SS cuts whole seconds, from the keyframe at or before the offset, so the
    frame is up to one GOP early (a second with a 1 s I-frame interval); the
    cut carries no finer time to correct by.
    """
    now = time.time()
    for rec in await client.recordings(camera_id, int(t) - 1, int(t) + 1):
        end = now if rec.live else rec.end
        if rec.start <= t < end:
            break
    else:
        return None
    raw = await client.download(rec.id, rec.mount_id, int((t - rec.start) * 1000), 1500)
    jpg = await _run_ffmpeg(
        ffmpeg,
        raw,
        lambda src: [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", src,
            "-frames:v", "1", "-vf", f"scale={int(width)}:-2",
            "-c:v", "mjpeg", "-q:v", "5", "-f", "image2", "pipe:1",
        ],
        timeout,
    )
    # Empty when the cut held no decodable frame (the last second of a file).
    return jpg or None


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
