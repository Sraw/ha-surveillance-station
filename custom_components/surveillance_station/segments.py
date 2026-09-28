"""Playback sessions' segments: fetched from the NAS on demand, one job per
segment, and kept in a byte-bounded cache.

Each segment is fetched on demand (``synology_ss_playback.fetch_segment``:
``Recording.Download`` cuts the range out of the recording on the NAS, ffmpeg
stream-copies it into fragmented MP4); a time-lapse one is transcoded
(``fetch_timelapse_segment``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from synology_ss_playback import (
    Segment,
    SSError,
    SurveillanceStationClient,
    TranscodeSpec,
    fetch_segment,
    fetch_timelapse_segment,
    render_playlist,
)

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant

from .const import MAX_PARALLEL_FETCHES, MAX_PARALLEL_TRANSCODES, SEGMENT_CACHE_BYTES
from .shared import ByteLRU, SharedJobs, retrieve_exception

# entry_id, recording_id, offset_ms, duration, media_start, spec: media_start
# is part of the key because the fragment timestamps depend on it, so only
# reloads of the same window share entries. The spec is too: time-lapse files
# and recordings number their ids separately, and the same cut transcodes
# differently per codec.
type SegmentKey = tuple[str, int, int, float, float, TranscodeSpec | None]


@dataclass(slots=True)
class VodSession:
    entry_id: str
    camera_id: int
    window_start: float
    segments: list[Segment]
    expires: float
    # A live session reaches the present; its playlist is an EVENT playlist
    # that grows (see VodManager.extend) until max_end.
    live: bool = False
    max_end: float = 0.0
    planned_end: float = 0.0
    # A time-lapse session: how each file's segments are transcoded (by
    # recording id). Empty for recordings, which are stream-copied.
    transcode: dict[int, TranscodeSpec] = field(default_factory=dict)
    playlist: str = field(init=False, default="")
    # Started live: its playlist stays an EVENT playlist once it stops growing
    # (RFC 8216 lets an EVENT playlist only be appended to, not turn VOD).
    event: bool = field(init=False, default=False)
    lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.event = self.live
        self.render()

    def render(self) -> None:
        self.playlist = render_playlist(self.segments, live=self.live, event=self.event)


class SegmentCache:
    """(init, media) of a session's segments, and the fetch queue behind them."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: Callable[[str], SurveillanceStationClient | None],
        track: Callable[[str, Exception | None], None],
    ) -> None:
        self.hass = hass
        self._client = client
        self._track = track
        self._cache: ByteLRU[SegmentKey, tuple[bytes, bytes]] = ByteLRU(
            SEGMENT_CACHE_BYTES, lambda parts: len(parts[0]) + len(parts[1])
        )
        # The fetches that got past the queue: those always finish, into the
        # cache (a time-lapse one only once its transcode runs, see fetch).
        self._started: set[asyncio.Task] = set()
        self._jobs: SharedJobs[SegmentKey, tuple[bytes, bytes]] = SharedJobs(
            hass, "vod", abandon=lambda key, task: task not in self._started or key[-1] is not None
        )
        self._sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)
        # Time-lapse segments: ~200 MB from the NAS and a GPU transcode each.
        self._transcode_sem = asyncio.Semaphore(MAX_PARALLEL_TRANSCODES)
        # ...of which one at a time on the GPU (see fetch_timelapse_segment).
        self._gpu = asyncio.Semaphore(1)
        self.transcoded = 0
        self.transcode_failures = 0

    def __len__(self) -> int:
        return len(self._cache)

    @property
    def bytes(self) -> int:
        return self._cache.bytes

    @property
    def in_flight(self) -> int:
        return len(self._jobs)

    def drop_entry(self, entry_id: str) -> None:
        self._cache.pop_where(lambda key: key[0] == entry_id)
        self._jobs.drop(lambda key: key[0] == entry_id)

    def cancel_timelapse(self) -> None:
        """Cancel the time-lapse segments on the way (a running transcode still finishes, see fetch)."""
        self._jobs.cancel(lambda key: key[-1] is not None)

    async def fetch(self, session: VodSession, seg: Segment) -> tuple[bytes, bytes]:
        spec = session.transcode.get(seg.recording_id) if session.transcode else None
        key = (
            session.entry_id, seg.recording_id, seg.offset_ms, round(seg.duration, 3), round(seg.media_start, 3), spec
        )
        if (hit := self._cache.get(key)) is not None:
            return hit
        # A client abort (a player may abort on every seek) neither kills the
        # work nor the other requests waiting for the same segment; a retry
        # then finds it cached. Everyone gave up on it before it reached the
        # NAS (a seek, a camera taken off the grid): it goes, so as not to
        # hold up the queue for the segments that are wanted now. A
        # time-lapse one goes even while downloading (~150 MB the new
        # position waits behind); its transcode, once running, finishes
        # regardless (see fetch_timelapse_segment).
        return await self._jobs.run(
            key,
            lambda: self._fetch_and_cache(key, session.entry_id, seg, spec),
            f"surveillance_station segment {seg.recording_id}@{seg.offset_ms}",
        )

    async def _fetch_and_cache(
        self, key: SegmentKey, entry_id: str, seg: Segment, spec: TranscodeSpec | None = None
    ) -> tuple[bytes, bytes]:
        me = asyncio.current_task()
        try:
            async with self._sem if spec is None else self._transcode_sem:
                self._started.add(me)
                if spec is None:
                    result = await self._fetch_uncached(entry_id, seg)
                else:
                    result = await self._fetch_transcoded(key, entry_id, seg, spec)
        finally:
            self._started.discard(me)
        self._cache.put(key, result)
        return result

    async def _fetch_transcoded(
        self, key: SegmentKey, entry_id: str, seg: Segment, spec: TranscodeSpec
    ) -> tuple[bytes, bytes]:
        turn = _GpuTurn(self._gpu)
        job = asyncio.ensure_future(self._fetch_uncached(entry_id, seg, spec, turn))
        job.add_done_callback(retrieve_exception)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError as err:
            if not (spec.hardware and turn.taken):
                job.cancel()
                raise
            cancelled = err
        # A hardware transcode, once running, finishes whoever leaves (see
        # fetch_timelapse_segment) and holds its cut until then: so this
        # permit is held until then too, and what it made is kept.
        result = await asyncio.shield(job)
        if self._client(entry_id) is not None:  # not unloaded meanwhile
            self._cache.put(key, result)
        raise cancelled

    async def _fetch_uncached(
        self, entry_id: str, seg: Segment, spec: TranscodeSpec | None = None, gpu: _GpuTurn | None = None
    ) -> tuple[bytes, bytes]:
        client = self._client(entry_id)
        if client is None:
            raise SSError("vod", "fetch", None, "Surveillance Station entry is not loaded")
        ffmpeg = get_ffmpeg_manager(self.hass).binary
        try:
            if spec is None:
                result = await fetch_segment(client, seg, ffmpeg)
            else:
                try:
                    result = await fetch_timelapse_segment(client, seg, ffmpeg, spec, gpu=gpu)
                except SSError:
                    self.transcode_failures += 1
                    raise
                self.transcoded += 1
        except SSError as err:
            self._track(entry_id, err)
            raise
        self._track(entry_id, None)
        return result


class _GpuTurn:
    """The GPU lock as fetch_timelapse_segment takes it for one segment: right
    before its transcode, released when that ends. Tells whether it began."""

    def __init__(self, lock: asyncio.Semaphore) -> None:
        self._lock = lock
        self.taken = False

    async def acquire(self) -> bool:
        await self._lock.acquire()
        self.taken = True
        return True

    def release(self) -> None:
        self._lock.release()
