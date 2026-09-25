"""HLS VOD endpoints backed by Surveillance Station recordings.

Flow: the card asks (over the authenticated WebSocket) for a playback window;
``VodManager.create_session`` plans the segments and returns an unguessable
token. The playlist and every segment URL live under that token, so the HTTP
views themselves need no HA auth header (hls.js cannot add one) - the token
*is* the capability, handed out only to authenticated WebSocket clients, and
it expires.

Each segment is fetched on demand: ``Recording.Download`` cuts the range out
of the recording on the NAS, ffmpeg stream-copies it into fragmented MP4 (no
transcoding), and the result is split into init + media parts.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import glob
import logging
import os
import secrets
import tempfile
import time

from aiohttp import web

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .api import SSError, SurveillanceStationClient
from .const import (
    MAX_PARALLEL_FETCHES,
    REMUX_TIMEOUT_SECONDS,
    SEGMENT_CACHE_BYTES,
    TEMP_PREFIX,
    VOD_MAX_SESSIONS,
    VOD_SESSION_TTL_SECONDS,
    VOD_URL,
)
from .vod import Recording, Segment, ffmpeg_remux_args, live_edge, plan_segments, render_playlist, split_fmp4

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
        self.clients: dict[str, SurveillanceStationClient] = {}
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
            client = self.clients.get(session.entry_id)
            if client is None:
                return
            try:
                last = session.segments[-1]
                infos = await client.recordings(
                    session.camera_id, int(last.wall_start + last.duration) - 60, int(new_end) + 1
                )
            except SSError as err:
                _LOGGER.debug("Live playlist not extended: %s", err)
                return
            # Append-only: whatever was published stays exactly as it was,
            # even if SS reports a file boundary late.
            added = plan_segments(to_recordings(infos), last.wall_start, new_end, now, after=last)
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
        client = self.clients.get(entry_id)
        if client is None:
            raise SSError("vod", "fetch", None, "Surveillance Station entry is not loaded")
        started = time.monotonic()
        # Ask for a little extra; SS rounds to keyframes and ffmpeg -t trims.
        raw = await client.download(
            seg.recording_id, seg.mount_id, seg.offset_ms, int(seg.duration * 1000) + 1000
        )
        fetched = time.monotonic()
        data = await self._remux(raw, seg)
        init, media = split_fmp4(data)
        if not init or not media:
            raise SSError("remux", "split", None, f"empty output for segment {seg.index}")
        _LOGGER.debug(
            "segment %s rec=%s off=%sms dur=%.1fs: download %.2fs (%d KB), remux %.2fs",
            seg.index, seg.recording_id, seg.offset_ms, seg.duration,
            fetched - started, len(raw) // 1024, time.monotonic() - fetched,
        )
        return init, media

    async def _remux(self, raw: bytes, seg: Segment) -> bytes:
        # SS puts the moov box at the end, so ffmpeg needs a seekable file.
        fd, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp4")
        proc = None
        try:
            await self.hass.async_add_executor_job(_write_and_close, fd, raw)
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_remux_args(
                    get_ffmpeg_manager(self.hass).binary, path, seg.duration, seg.media_start, seg.hevc
                ),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(REMUX_TIMEOUT_SECONDS):
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
            await self.hass.async_add_executor_job(_unlink, path)


def remove_stale_temp_files() -> int:
    """Delete scratch files a crash (or a killed container) left behind.

    Every remux unlinks its file when done, so at startup none can be in use.
    """
    removed = 0
    for path in glob.glob(os.path.join(tempfile.gettempdir(), f"{TEMP_PREFIX}*.mp4")):
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def to_recordings(infos) -> list[Recording]:
    return [Recording(r.id, r.start, r.end, r.mount_id, r.live, r.hevc) for r in infos]


def _retrieve_exception(task: asyncio.Task) -> None:
    # Everyone waiting may have gone away; don't log "never retrieved".
    if not task.cancelled():
        task.exception()


def _write_and_close(fd: int, data: bytes) -> None:
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


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
        except SSError as err:  # includes connection errors, see api.py
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
