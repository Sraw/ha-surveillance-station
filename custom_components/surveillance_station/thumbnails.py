"""Event thumbnails and notification images: frames of the recordings, made once and kept."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time
from typing import NamedTuple

from synology_ss_playback import RecordingInfo, SSError, SurveillanceStationClient, fetch_snapshot

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    LARGE_IMAGE_DISK_BYTES,
    MAX_PARALLEL_THUMBNAILS,
    NOT_RECORDING_GRACE_SECONDS,
    RECORDING_GAP_SECONDS,
    THUMBNAIL_CACHE_BYTES,
    THUMBNAIL_DISK_BYTES,
    THUMBNAIL_ENTRY_BYTES,
    THUMBNAIL_MISS_SECONDS,
    THUMBNAIL_POLL_SECONDS,
    THUMBNAIL_RECENT_MISS_SECONDS,
    THUMBNAIL_WIDTH,
)
from .shared import ByteLRU, SharedJobs
from .thumbnail_store import ThumbnailStore

# Patchable in tests (time.monotonic itself is the event loop's clock).
_monotonic = time.monotonic

type _Key = tuple[str, int, int, int]  # entry_id, camera_id, ts, width


class _Made(NamedTuple):
    at: float
    jpeg: bytes  # b"": nothing recorded then


class ThumbnailService:
    """Frames by camera and moment: in memory, on disk, else cut from the recording."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: Callable[[str], SurveillanceStationClient | None],
        track: Callable[[str, Exception | None], None],
    ) -> None:
        self.hass = hass
        self._client = client
        self._track = track
        # Misses count too (a fixed cost per entry): see THUMBNAIL_ENTRY_BYTES.
        self._made: ByteLRU[_Key, _Made] = ByteLRU(
            THUMBNAIL_CACHE_BYTES, lambda m: len(m.jpeg) + THUMBNAIL_ENTRY_BYTES
        )
        self._jobs: SharedJobs[_Key, bytes | None] = SharedJobs(hass, "thumbnail", abandon=lambda key, task: True)
        self._sem = asyncio.Semaphore(MAX_PARALLEL_THUMBNAILS)
        self.disk = ThumbnailStore(hass, hass.config.cache_path(DOMAIN, "thumbnails"), THUMBNAIL_DISK_BYTES)
        self.disk_large = ThumbnailStore(hass, hass.config.cache_path(DOMAIN, "images"), LARGE_IMAGE_DISK_BYTES)

    def __len__(self) -> int:
        return len(self._made)

    @property
    def bytes(self) -> int:
        return self._made.bytes

    async def load(self) -> None:
        await self.disk.load()
        await self.disk_large.load()

    def drop_entry(self, entry_id: str) -> None:
        self._made.pop_where(lambda key: key[0] == entry_id)
        self._jobs.drop(lambda key: key[0] == entry_id)

    async def when_recorded(
        self, entry_id: str, camera_id: int, ts: int, wait: float, width: int = THUMBNAIL_WIDTH
    ) -> bytes | None:
        """The frame at ts once SS has written it, waiting at most wait seconds.

        SS moves a recording's reported end forward every ~10 s, to what it
        has written: until that passes ts, a cut there could come out short
        (an earlier keyframe) or empty, and that would be kept.
        """
        deadline = _monotonic() + wait
        grace = _monotonic() + min(wait, NOT_RECORDING_GRACE_SECONDS)
        while True:
            client = self._client(entry_id)
            try:
                if client is not None:
                    recordings = await client.recordings(camera_id, ts - RECORDING_GAP_SECONDS, ts + 1)
                    if (rec := next((r for r in recordings if r.start <= ts and r.end >= ts + 1), None)) is not None:
                        # The recording found here is the one cut: not listed again.
                        return await self.thumbnail(entry_id, camera_id, ts, width, recording=rec)
                    if _monotonic() >= grace and not any(
                        r.live or r.end >= ts - RECORDING_GAP_SECONDS for r in recordings
                    ):
                        # SS isn't recording this camera (motion-only, and
                        # its trigger not come by now; or not at all):
                        # nothing to wait for.
                        return None
            except SSError:
                pass
            if _monotonic() + THUMBNAIL_POLL_SECONDS > deadline:
                return None
            await asyncio.sleep(THUMBNAIL_POLL_SECONDS)

    async def thumbnail(
        self, entry_id: str, camera_id: int, ts: int, width: int = THUMBNAIL_WIDTH,
        recording: RecordingInfo | None = None,
    ) -> bytes | None:
        """JPEG of camera_id at ts, width pixels wide, or None if nothing was recorded then.

        Requests for the same frame share one job; a job nobody waits for any
        more (the thumbnail was scrolled past) is cancelled. recording: the one
        holding ts, if the caller has found it already.
        """
        key = (entry_id, camera_id, ts, width)
        if (hit := self._made.get(key)) is not None:
            # A miss is only remembered briefly: the recording may just not
            # have been listed yet (a moment just past: a few seconds).
            recent = time.time() - ts < RECORDING_GAP_SECONDS
            if hit.jpeg or _monotonic() - hit.at < (THUMBNAIL_RECENT_MISS_SECONDS if recent else THUMBNAIL_MISS_SECONDS):
                return hit.jpeg or None
            self._made.pop(key)
        return await self._jobs.run(
            key, lambda: self._make(key, recording), f"surveillance_station thumbnail {camera_id}@{ts}"
        )

    async def _make(self, key: _Key, recording: RecordingInfo | None = None) -> bytes | None:
        entry_id, camera_id, ts, width = key
        disk = self.disk if width == THUMBNAIL_WIDTH else self.disk_large
        # Made before (even before a restart): no need for the NAS.
        if (jpg := await disk.get(key[:3])) is None:
            client = self._client(entry_id)
            if client is None:
                raise SSError("thumbnail", "fetch", None, "Surveillance Station entry is not loaded")
            async with self._sem:
                try:
                    jpg = await fetch_snapshot(
                        client, camera_id, ts, get_ffmpeg_manager(self.hass).binary, width, recording=recording
                    )
                except SSError as err:
                    self._track(entry_id, err)
                    raise
            self._track(entry_id, None)
            if jpg:  # "nothing recorded" is only remembered in memory, briefly
                disk.put(key[:3], jpg)
        self._made.put(key, _Made(_monotonic(), jpg or b""))
        return jpg or None
