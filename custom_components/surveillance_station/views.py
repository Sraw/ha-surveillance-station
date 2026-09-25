"""HLS VOD endpoints backed by Surveillance Station recordings.

Flow: the card asks (over the authenticated WebSocket) for a playback window;
``VodManager.create_session`` plans the segments and returns an unguessable
token. The playlist and every segment URL live under that token, so the HTTP
views themselves need no HA auth header (hls.js cannot add one) - the token
*is* the capability, handed out only to authenticated WebSocket clients, and
it expires.

Each segment is fetched on demand (``synology_ss_playback.fetch_segment``:
``Recording.Download`` cuts the range out of the recording on the NAS, ffmpeg
stream-copies it into fragmented MP4). This module owns what is HA-specific:
the sessions, the segment cache and fetch queue, and the HTTP views.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import logging
import secrets
import time

from aiohttp import web
from synology_ss_playback import (
    Segment,
    SSConnectionError,
    SSError,
    SurveillanceStationClient,
    fetch_segment,
    live_edge,
    plan_segments,
    recordings_from,
    render_playlist,
)

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util.hass_dict import HassKey

from .const import (
    DOMAIN,
    MAX_PARALLEL_FETCHES,
    SEGMENT_CACHE_BYTES,
    VOD_MAX_SESSIONS,
    VOD_SESSION_TTL_SECONDS,
    VOD_URL,
)

_LOGGER = logging.getLogger(__name__)


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
    playlist: str = field(init=False, default="")
    lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.render()

    def render(self) -> None:
        self.playlist = render_playlist(self.segments, live=self.live)


class VodManager:
    """Holds playback sessions and turns segments into fMP4 bytes."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # Entries whose Surveillance Station is known to be unreachable (logged once).
        self._unreachable: set[str] = set()
        self._sessions: OrderedDict[str, VodSession] = OrderedDict()
        # key -> (init, media). media_start is part of the key because the
        # fragment timestamps depend on it, so only reloads of the same window
        # share entries.
        self._cache: OrderedDict[tuple, tuple[bytes, bytes]] = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[tuple, asyncio.Task] = {}
        # Requests currently waiting on each in-flight fetch, and the fetches
        # that got past the queue (those always finish, into the cache).
        self._waiters: dict[tuple, int] = {}
        self._started: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    def client(self, entry_id: str) -> SurveillanceStationClient | None:
        """The logged-in client of a loaded entry."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return None
        return entry.runtime_data

    def track(self, entry_id: str, err: Exception | None) -> None:
        """Log once when Surveillance Station goes away, and once when it's back."""
        if isinstance(err, SSConnectionError):
            if entry_id not in self._unreachable:
                self._unreachable.add(entry_id)
                _LOGGER.warning("Surveillance Station is unreachable: %s", err)
        elif err is None and entry_id in self._unreachable:
            self._unreachable.discard(entry_id)
            _LOGGER.info("Surveillance Station is reachable again")

    def drop_entry(self, entry_id: str) -> None:
        """Forget an unloaded entry's sessions and segments."""
        for token in [t for t, s in self._sessions.items() if s.entry_id == entry_id]:
            del self._sessions[token]
        for key in [k for k in self._cache if k[0] == entry_id]:
            init, media = self._cache.pop(key)
            self._cache_bytes -= len(init) + len(media)
        for key, task in list(self._inflight.items()):
            if key[0] == entry_id:
                task.cancel()
        self._unreachable.discard(entry_id)

    def stats(self) -> dict[str, int]:
        return {
            "sessions": len(self._sessions),
            "cached_segments": len(self._cache),
            "cached_bytes": self._cache_bytes,
            "fetches_in_flight": len(self._inflight),
        }

    def create_session(self, session: VodSession) -> str:
        now = time.time()
        for token in [t for t, s in self._sessions.items() if s.expires < now]:
            del self._sessions[token]
        while len(self._sessions) >= VOD_MAX_SESSIONS:
            self._sessions.popitem(last=False)  # least recently used
        token = secrets.token_urlsafe(32)
        self._sessions[token] = session
        return token

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
        key = (session.entry_id, seg.recording_id, seg.offset_ms, round(seg.duration, 3), round(seg.media_start, 3))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        task = self._inflight.get(key)
        if task is None or task.cancelling():
            # The fetch runs as its own task so a client abort (hls.js aborts
            # on every seek) neither kills the work nor the other requests
            # waiting for the same segment; a retry then finds it cached.
            task = self.hass.async_create_background_task(
                self._fetch_and_cache(key, session.entry_id, seg),
                f"surveillance_station segment {seg.recording_id}@{seg.offset_ms}",
            )
            task.add_done_callback(_retrieve_exception)
            self._inflight[key] = task
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            return await asyncio.shield(task)
        finally:
            if left := self._waiters.pop(key) - 1:
                self._waiters[key] = left
            elif not task.done() and task not in self._started:
                # Everyone gave up on it before it reached the NAS (a seek, a
                # camera taken off the grid): don't let it hold up the queue
                # for the segments that are wanted now.
                task.cancel()

    async def _fetch_and_cache(self, key: tuple, entry_id: str, seg: Segment) -> tuple[bytes, bytes]:
        me = asyncio.current_task()
        try:
            async with self._sem:
                self._started.add(me)
                result = await self._fetch_uncached(entry_id, seg)
        finally:
            self._started.discard(me)
            if self._inflight.get(key) is me:
                del self._inflight[key]
        self._cache[key] = result
        self._cache_bytes += len(result[0]) + len(result[1])
        while self._cache_bytes > SEGMENT_CACHE_BYTES and len(self._cache) > 1:
            _, (init, media) = self._cache.popitem(last=False)
            self._cache_bytes -= len(init) + len(media)
        return result

    async def _fetch_uncached(self, entry_id: str, seg: Segment) -> tuple[bytes, bytes]:
        client = self.client(entry_id)
        if client is None:
            raise SSError("vod", "fetch", None, "Surveillance Station entry is not loaded")
        try:
            result = await fetch_segment(client, seg, get_ffmpeg_manager(self.hass).binary)
        except SSError as err:
            self.track(entry_id, err)
            raise
        self.track(entry_id, None)
        return result


DATA_MANAGER: HassKey[VodManager] = HassKey(DOMAIN)


def _retrieve_exception(task: asyncio.Task) -> None:
    # Everyone waiting may have gone away; don't log "never retrieved".
    if not task.cancelled():
        task.exception()


# Every session has its own URLs, so a cached segment is never asked for again
# once its session is gone; storing it would only fill the client's disk cache
# (~2 GB per hour of a 4-camera grid). hls.js keeps what it needs in memory.
_NO_STORE = {"Cache-Control": "no-store"}


class _VodBaseView(HomeAssistantView):
    # The token in the path is the credential; see module docstring.
    requires_auth = False

    def __init__(self, manager: VodManager) -> None:
        self.manager = manager

    def _session(self, token: str) -> VodSession:
        session = self.manager.get_session(token)
        if session is None:
            raise web.HTTPNotFound()
        return session

    @staticmethod
    def _segment(session: VodSession, index: str) -> Segment:
        i = int(index)
        if not 0 <= i < len(session.segments):
            raise web.HTTPNotFound()
        return session.segments[i]

    async def _get_parts(self, session: VodSession, seg: Segment) -> tuple[bytes, bytes]:
        try:
            return await self.manager.fetch(session, seg)
        except SSConnectionError:
            raise web.HTTPBadGateway() from None  # logged once by VodManager.track
        except SSError as err:
            _LOGGER.warning("Segment %s of recording %s failed: %s", seg.index, seg.recording_id, err)
            raise web.HTTPBadGateway() from None


class VodPlaylistView(_VodBaseView):
    url = VOD_URL + "/{token}/index.m3u8"
    name = "api:surveillance_station:vod:playlist"

    async def get(self, request: web.Request, token: str) -> web.Response:
        session = self._session(token)
        await self.manager.extend(session)
        return web.Response(
            text=session.playlist,
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )


class VodInitView(_VodBaseView):
    url = VOD_URL + r"/{token}/init/{index:\d+}.mp4"
    name = "api:surveillance_station:vod:init"

    async def get(self, request: web.Request, token: str, index: str) -> web.Response:
        session = self._session(token)
        init, _ = await self._get_parts(session, self._segment(session, index))
        return web.Response(body=init, content_type="video/mp4", headers=_NO_STORE)


class VodSegmentView(_VodBaseView):
    url = VOD_URL + r"/{token}/seg/{index:\d+}.m4s"
    name = "api:surveillance_station:vod:segment"

    async def get(self, request: web.Request, token: str, index: str) -> web.Response:
        session = self._session(token)
        _, media = await self._get_parts(session, self._segment(session, index))
        return web.Response(body=media, content_type="video/iso.segment", headers=_NO_STORE)
