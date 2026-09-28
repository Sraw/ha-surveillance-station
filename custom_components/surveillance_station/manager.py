"""VodManager: the one object the rest of the integration asks for playback.

It holds the playback sessions, the GPU check, the time-lapse file list, the
bookmark frames and what the Surveillance Station of each entry answers
(unreachable: logged once, a Repairs issue if it lasts), and hands the rest
to focused parts: the segment cache (segments.py), thumbnails
(thumbnails.py), signed URLs and live-stream tokens (tokens.py), and the
bookmark list (bookmarks.py).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import secrets
import time
from typing import Any

import aiohttp
from aiohttp import web
from synology_ss_playback import (
    Bookmark,
    Segment,
    SSConnectionError,
    SSError,
    SurveillanceStationClient,
    TimelapseRecording,
    hardware_transcode_available,
    live_edge,
    plan_segments,
    recordings_from,
)

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .bookmarks import BookmarkCache, BookmarkIndex
from .const import (
    BOOKMARK_FRAMES_MAX,
    DOMAIN,
    SS_ISSUE_AFTER_SECONDS,
    THUMBNAIL_WIDTH,
    TIMELAPSE_LIST_SECONDS,
    VOD_MAX_SESSIONS,
    VOD_SESSION_TTL_SECONDS,
)
from .segments import SegmentCache, VodSession
from .thumbnail_store import ThumbnailStore
from .thumbnails import ThumbnailService
from .tokens import LiveTokens, UrlSigner

_LOGGER = logging.getLogger(__name__)

# Patchable in tests (time.monotonic itself is the event loop's clock).
_monotonic = time.monotonic


class VodManager:
    """Holds playback sessions and turns segments into fMP4 bytes."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # Entries whose Surveillance Station is known to be unreachable (logged
        # once), since when; and those with a Repairs issue for it.
        self._unreachable: dict[str, float] = {}
        self._unreachable_issues: set[str] = set()
        self._sessions: OrderedDict[str, VodSession] = OrderedDict()
        # Time-lapse sessions ended by a newer one (answered 410, not 404, so
        # a player doesn't open its day again and end the newer one in turn).
        self.superseded: OrderedDict[str, None] = OrderedDict()
        self.segments = SegmentCache(hass, self.client, self.track)
        self.thumbnails = ThumbnailService(hass, self.client, self.track)
        self.signer = UrlSigner(hass)
        self.bookmark_cache = BookmarkCache(hass, self.track)
        self.live_tokens = LiveTokens()
        self._hardware: bool | None = None
        self._hardware_task: asyncio.Task | None = None
        # entry_id -> (fetched at, every time-lapse file).
        self._timelapse: dict[str, tuple[float, list[TimelapseRecording]]] = {}
        # One listing per entry at a time (a NAS that hangs holds up only its own).
        self._timelapse_locks: dict[str, asyncio.Lock] = {}
        # "entry_id/bookmark_id" -> the moment its thumbnail shows (see set_frame), oldest first.
        self._frames: OrderedDict[str, int] = OrderedDict()
        self._frame_store: Store[dict[str, int]] = Store(hass, 1, f"{DOMAIN}.bookmark_frames")
        # The live relays running: (entry_id, browser socket).
        self.live_streams: set[tuple[str, web.WebSocketResponse]] = set()

    @property
    def disk(self) -> ThumbnailStore:
        return self.thumbnails.disk

    @property
    def disk_large(self) -> ThumbnailStore:
        return self.thumbnails.disk_large

    async def async_load(self) -> None:
        """Load the thumbnail signing key (made on first use) and the disk cache's index."""
        await self.signer.load()
        await self.thumbnails.load()
        frames = await self._frame_store.async_load()
        if isinstance(frames, dict):
            self._frames.update((k, v) for k, v in frames.items() if isinstance(v, int))

    def set_frame(self, entry_id: str, bookmark_id: int, ts: int) -> None:
        """Show the bookmark as the moment ts (where a detector saw it best), not its start."""
        key = f"{entry_id}/{bookmark_id}"
        if self._frames.get(key) == ts:
            return
        self._frames[key] = ts
        self._frames.move_to_end(key)
        while len(self._frames) > BOOKMARK_FRAMES_MAX:
            self._frames.popitem(last=False)
        self._frame_store.async_delay_save(lambda: dict(self._frames), 10)

    def frame(self, entry_id: str, bookmark: Bookmark) -> int:
        """The moment a bookmark's thumbnail shows: a second in, unless a detector picked one."""
        return self._frames.get(f"{entry_id}/{bookmark.id}", bookmark.start + 1)

    async def forget_frames(self, entry_id: str) -> None:
        """A removed entry's bookmarks: their frames aren't kept any more."""
        prefix = f"{entry_id}/"
        for key in [k for k in self._frames if k.startswith(prefix)]:
            del self._frames[key]
        await self._frame_store.async_save(dict(self._frames))

    def client(self, entry_id: str) -> SurveillanceStationClient | None:
        """The logged-in client of a loaded entry."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return None
        return entry.runtime_data

    def track(self, entry_id: str, err: Exception | None) -> None:
        """Log once when Surveillance Station goes away, and once when it's back.

        Still unreachable SS_ISSUE_AFTER_SECONDS later (asked again then,
        not just once), it's a Repairs issue until it answers. None only for
        what SS itself just answered: a cached answer, or a command that never
        asks SS (a live token), would keep an outage from ever lasting.
        """
        if isinstance(err, SSConnectionError):
            if (since := self._unreachable.get(entry_id)) is None:
                self._unreachable[entry_id] = _monotonic()
                _LOGGER.warning("Surveillance Station is unreachable: %s", err)
            elif _monotonic() - since >= SS_ISSUE_AFTER_SECONDS and entry_id not in self._unreachable_issues:
                entry = self.hass.config_entries.async_get_entry(entry_id)
                self._unreachable_issues.add(entry_id)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    _unreachable_issue(entry_id),
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="ss_unreachable",
                    translation_placeholders={
                        "name": entry.title if entry is not None else entry_id,
                        "error": str(err)[:300],
                    },
                )
        elif err is None and entry_id in self._unreachable:
            del self._unreachable[entry_id]
            self._clear_unreachable_issue(entry_id)
            _LOGGER.info("Surveillance Station is reachable again")

    def _clear_unreachable_issue(self, entry_id: str) -> None:
        if entry_id in self._unreachable_issues:
            self._unreachable_issues.discard(entry_id)
            ir.async_delete_issue(self.hass, DOMAIN, _unreachable_issue(entry_id))

    def drop_entry(self, entry_id: str) -> None:
        """Forget an unloaded entry's sessions and segments."""
        for token in [t for t, s in self._sessions.items() if s.entry_id == entry_id]:
            del self._sessions[token]
        self.segments.drop_entry(entry_id)
        self.thumbnails.drop_entry(entry_id)
        self.bookmark_cache.drop_entry(entry_id)
        self.live_tokens.drop_entry(entry_id)
        for stream in [x for x in self.live_streams if x[0] == entry_id]:
            # One still connecting to SS isn't prepared (close() would raise);
            # it notices the entry is gone once connected.
            if stream[1].prepared:
                self.hass.async_create_task(stream[1].close(code=aiohttp.WSCloseCode.GOING_AWAY))
        # An unloaded entry's outage is no issue any more (a reload looks afresh).
        self._unreachable.pop(entry_id, None)
        self._clear_unreachable_issue(entry_id)
        self._timelapse.pop(entry_id, None)
        self._timelapse_locks.pop(entry_id, None)

    def stats(self) -> dict[str, Any]:
        return {
            "sessions": len(self._sessions),
            "cached_segments": len(self.segments),
            "cached_bytes": self.segments.bytes,
            "fetches_in_flight": self.segments.in_flight,
            "cached_thumbnails": len(self.thumbnails),
            "cached_thumbnail_bytes": self.thumbnails.bytes,
            "disk_thumbnails": len(self.disk),
            "disk_thumbnail_bytes": self.disk.bytes,
            "disk_images": len(self.disk_large),
            "disk_image_bytes": self.disk_large.bytes,
            "live_streams": len(self.live_streams),
            "live_tokens": len(self.live_tokens),
            "timelapse_hardware": self._hardware,
            "timelapse_transcoded": self.segments.transcoded,
            "timelapse_transcode_failures": self.segments.transcode_failures,
        }

    async def hardware(self) -> bool | None:
        """Whether video can be transcoded on an Intel GPU (QSV) here.

        Checked once. None: the check couldn't tell (timed out, the GPU busy)
        - never taken for "no GPU"; the next caller checks again. One check
        at a time, shared by everyone asking, and not abandoned by a caller
        leaving.
        """
        if self.check_hardware() is not None:
            return self._hardware
        return await asyncio.shield(self._hardware_task)

    def check_hardware(self) -> bool | None:
        """The GPU check's answer if there is one; otherwise None, the check started (not waited for)."""
        if self._hardware is None and self._hardware_task is None:
            self._hardware_task = self.hass.async_create_background_task(
                self._check_hardware(), "surveillance_station GPU check", eager_start=False
            )
        return self._hardware

    async def _check_hardware(self) -> bool | None:
        try:
            found = await hardware_transcode_available(get_ffmpeg_manager(self.hass).binary)
        finally:
            self._hardware_task = None
        if found is None:
            _LOGGER.warning("The Intel GPU check timed out (busy?); checked again on the next time-lapse")
        else:
            self._hardware = found
            _LOGGER.info("Intel GPU (QSV) for transcoding: %s", "yes" if found else "none usable")
        return found

    async def timelapse_files(self, entry_id: str, client: SurveillanceStationClient) -> list[TimelapseRecording]:
        """Every time-lapse file of the entry's NAS, at most TIMELAPSE_LIST_SECONDS old."""
        async with self._timelapse_locks.setdefault(entry_id, asyncio.Lock()):
            hit = self._timelapse.get(entry_id)
            if hit is not None and _monotonic() - hit[0] < TIMELAPSE_LIST_SECONDS:
                return hit[1]
            try:
                files = await client.timelapse_recordings()
            except SSError as err:
                self.track(entry_id, err)
                raise
            self.track(entry_id, None)
            self._timelapse[entry_id] = (_monotonic(), files)
            return files

    async def bookmarks(self, entry_id: str, client: SurveillanceStationClient) -> list[Bookmark]:
        """Every bookmark of every camera, newest first, at most a minute old."""
        return (await self.bookmark_cache.index(entry_id, client)).all

    async def bookmark_index(self, entry_id: str, client: SurveillanceStationClient) -> BookmarkIndex:
        """The same bookmarks, indexed for the event list (see BookmarkCache)."""
        return await self.bookmark_cache.index(entry_id, client)

    def forget_bookmarks(self, entry_id: str) -> None:
        """Changed in SS by us: list them afresh on the next request."""
        self.bookmark_cache.forget(entry_id)

    async def thumbnail_when_recorded(
        self, entry_id: str, camera_id: int, ts: int, wait: float, width: int = THUMBNAIL_WIDTH
    ) -> bytes | None:
        """The frame at ts once SS has written it (see ThumbnailService.when_recorded)."""
        return await self.thumbnails.when_recorded(entry_id, camera_id, ts, wait, width)

    async def thumbnail(
        self, entry_id: str, camera_id: int, ts: int, width: int = THUMBNAIL_WIDTH
    ) -> bytes | None:
        """JPEG of camera_id at ts, or None if nothing was recorded then (see ThumbnailService.thumbnail)."""
        return await self.thumbnails.thumbnail(entry_id, camera_id, ts, width)

    def sign_thumbnail(self, entry_id: str, camera_id: int, ts: int, large: bool = False) -> str:
        """A URL for the frame of camera_id at ts, valid for one to two days (see UrlSigner)."""
        return self.signer.sign_thumbnail(entry_id, camera_id, ts, large)

    def sign_path(self, path: str) -> str:
        """Any image URL of ours, signed as sign_thumbnail's are (checked by check_thumbnail)."""
        return self.signer.sign_path(path)

    def check_thumbnail(self, path: str, exp: str | None, sig: str | None) -> bool:
        return self.signer.check(path, exp, sig)

    def create_live_token(self, entry_id: str, camera_id: int, at: float | None = None) -> str:
        """A single-use token for a camera's stream: real time, or recordings from ``at``."""
        return self.live_tokens.create(entry_id, camera_id, at)

    def take_live_token(self, token: str) -> tuple[str, int, float | None] | None:
        """(entry_id, camera_id, at) of an unused, unexpired token; it is used up."""
        return self.live_tokens.take(token)

    def create_session(self, session: VodSession) -> str:
        now = time.time()
        for token in [t for t, s in self._sessions.items() if s.expires < now]:
            del self._sessions[token]
        while len(self._sessions) >= VOD_MAX_SESSIONS:
            self._sessions.popitem(last=False)  # least recently used
        token = secrets.token_urlsafe(32)
        self._sessions[token] = session
        return token

    def create_timelapse_session(self, session: VodSession) -> str:
        """Like create_session, but the only time-lapse session: one stream at a time.

        Each is a heavy transcode fed from a ~30 MB/s cut on the NAS, so an
        earlier one (another camera or day, another viewer) ends here: its
        playlist and segments are gone and its segments on the way cancelled.
        """
        for token in [t for t, s in self._sessions.items() if s.transcode]:
            del self._sessions[token]
            self.superseded[token] = None
        while len(self.superseded) > VOD_MAX_SESSIONS:
            self.superseded.popitem(last=False)
        self.segments.cancel_timelapse()
        return self.create_session(session)

    def get_session(self, token: str) -> VodSession | None:
        session = self._sessions.get(token)
        now = time.time()
        if session is None or session.expires < now:
            return None
        # Sessions in use stay alive and at the back of the eviction queue.
        session.expires = now + VOD_SESSION_TTL_SECONDS
        self._sessions.move_to_end(token)
        return session

    async def extend(self, session: VodSession) -> None:
        """Append the segments recorded since the live playlist was last planned."""
        if not session.live:
            return
        async with session.lock:
            now = time.time()
            new_end = min(live_edge(now), session.max_end)
            if new_end <= session.planned_end:
                return
            client = self.client(session.entry_id)
            if client is None:
                return
            try:
                last = session.segments[-1]
                infos = await client.recordings(
                    session.camera_id, int(last.wall_start + last.duration) - 60, int(new_end) + 1
                )
            except SSError as err:
                self.track(session.entry_id, err)
                _LOGGER.debug("Live playlist not extended: %s", err)
                return
            self.track(session.entry_id, None)
            # Append-only: whatever was published stays exactly as it was,
            # even if SS reports a file boundary late.
            added = plan_segments(recordings_from(infos), last.wall_start, new_end, now, after=last)
            session.segments = session.segments + added
            session.planned_end = new_end
            if new_end >= session.max_end:
                session.live = False
            session.render()

    async def fetch(self, session: VodSession, seg: Segment) -> tuple[bytes, bytes]:
        """(init, media) of a session's segment (see SegmentCache.fetch)."""
        return await self.segments.fetch(session, seg)


DATA_MANAGER: HassKey[VodManager] = HassKey(DOMAIN)


def _unreachable_issue(entry_id: str) -> str:
    return f"ss_unreachable_{entry_id}"
