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
    SEGMENT_CACHE_SIZE,
    VOD_MAX_SESSIONS,
    VOD_SESSION_TTL_SECONDS,
    VOD_URL,
)
from .vod import Segment, ffmpeg_remux_args, render_playlist, split_fmp4

_LOGGER = logging.getLogger(__name__)


@dataclass
class VodSession:
    entry_id: str
    camera_id: int
    segments: list[Segment]
    expires: float
    playlist: str = field(init=False)

    def __post_init__(self) -> None:
        self.playlist = render_playlist(self.segments)


class VodManager:
    """Holds playback sessions and turns segments into fMP4 bytes."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.clients: dict[str, SurveillanceStationClient] = {}
        self._sessions: OrderedDict[str, VodSession] = OrderedDict()
        # key -> (init, media); key identifies the cut, not the session, so
        # two sessions over the same moment share work.
        self._cache: OrderedDict[tuple, tuple[bytes, bytes]] = OrderedDict()
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    def create_session(self, entry_id: str, camera_id: int, segments: list[Segment]) -> str:
        now = time.time()
        for token in [t for t, s in self._sessions.items() if s.expires < now]:
            del self._sessions[token]
        while len(self._sessions) >= VOD_MAX_SESSIONS:
            self._sessions.popitem(last=False)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = VodSession(entry_id, camera_id, segments, now + VOD_SESSION_TTL_SECONDS)
        return token

    def get_session(self, token: str) -> VodSession | None:
        session = self._sessions.get(token)
        if session is None or session.expires < time.time():
            return None
        return session

    async def fetch(self, session: VodSession, seg: Segment) -> tuple[bytes, bytes]:
        key = (session.entry_id, seg.recording_id, seg.offset_ms, round(seg.duration, 3), round(seg.media_start, 3))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if key in self._inflight:
            return await self._inflight[key]
        fut: asyncio.Future = self.hass.loop.create_future()
        self._inflight[key] = fut
        try:
            result = await self._fetch_uncached(session.entry_id, seg)
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as err:
            fut.set_exception(err)
            fut.exception()  # mark retrieved; waiters re-raise it
            raise
        finally:
            self._inflight.pop(key, None)
        fut.set_result(result)
        self._cache[key] = result
        while len(self._cache) > SEGMENT_CACHE_SIZE:
            self._cache.popitem(last=False)
        return result

    async def _fetch_uncached(self, entry_id: str, seg: Segment) -> tuple[bytes, bytes]:
        client = self.clients[entry_id]
        async with self._sem:
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
        fd, path = tempfile.mkstemp(prefix="ss_vod_", suffix=".mp4")
        try:
            await self.hass.async_add_executor_job(_write_and_close, fd, raw)
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_remux_args(
                    get_ffmpeg_manager(self.hass).binary, path, seg.duration, seg.media_start, seg.hevc
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                raise SSError("ffmpeg", "remux", proc.returncode, err.decode(errors="replace")[-400:])
            return out
        finally:
            await self.hass.async_add_executor_job(_unlink, path)


def _write_and_close(fd: int, data: bytes) -> None:
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


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
        except SSError as err:
            _LOGGER.warning("Segment %s of recording %s failed: %s", seg.index, seg.recording_id, err)
            raise web.HTTPBadGateway() from err


class VodPlaylistView(_VodBaseView):
    url = VOD_URL + "/{token}/index.m3u8"
    name = "api:surveillance_station:vod:playlist"

    async def get(self, request: web.Request, token: str) -> web.Response:
        session = self._session(token)
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
        return web.Response(body=init, content_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})


class VodSegmentView(_VodBaseView):
    url = VOD_URL + r"/{token}/seg/{index:\d+}.m4s"
    name = "api:surveillance_station:vod:segment"

    async def get(self, request: web.Request, token: str, index: str) -> web.Response:
        session = self._session(token)
        _, media = await self._get_parts(session, self._segment(session, index))
        return web.Response(body=media, content_type="video/iso.segment", headers={"Cache-Control": "private, max-age=3600"})
