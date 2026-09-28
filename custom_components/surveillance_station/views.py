"""HTTP views: SS's stream relay, event thumbnails, and HLS VOD endpoints.

The card plays video from SS's own stream (LiveStreamView), live or recorded,
wherever the browser has MSE. The HLS endpoints serve browsers without it.

HLS flow: the card asks (over the authenticated WebSocket) for a playback window;
``VodManager.create_session`` plans the segments and returns an unguessable
token. The playlist and every segment URL live under that token, so the HTTP
views themselves need no HA auth header (a <video> cannot add one) - the token
*is* the capability, handed out only to authenticated WebSocket clients, and
it expires.

The sessions, segment cache and fetch queue are VodManager's (manager.py,
segments.py); this module is the HTTP views over them.
"""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp
from aiohttp import web
from synology_ss_playback import Segment, SSConnectionError, SSError

from homeassistant.components.http import HomeAssistantView

from .const import (
    LARGE_IMAGE_WIDTH,
    LIVE_IDLE_SECONDS,
    LIVE_KEEP_ALIVE_SECONDS,
    LIVE_URL,
    MAX_LIVE_STREAMS,
    THUMBNAIL_URL,
    THUMBNAIL_WIDTH,
    VOD_URL,
)
from .manager import VodManager
from .segments import VodSession

_LOGGER = logging.getLogger(__name__)


class LiveStreamView(HomeAssistantView):
    """A camera's real-time stream, relayed from Surveillance Station.

    The browser opens a WebSocket here with a single-use token from the
    ``surveillance_station/live`` command (the token is the credential, as
    for the VOD views); HA opens the documented SS stream socket with its own
    session and passes the messages through unchanged. The SS session id
    never reaches the browser, and the stream works wherever HA is reachable.
    The browser may only steer playback: ``time=<epoch>``, ``pause=true|false``
    and ``speed=<0.5|1|2|4|8|16>`` (the card's speeds, and 16 to get over a
    hole in a recording) are passed on, nothing else.
    """

    requires_auth = False
    url = LIVE_URL + "/{token}"
    name = "api:surveillance_station:live"

    def __init__(self, manager: VodManager) -> None:
        self.manager = manager

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        if (found := self.manager.take_live_token(token)) is None:
            raise web.HTTPNotFound()
        entry_id, camera_id, at = found
        # No heartbeat: video flows all the time, and the browser's
        # keep-alives (or their absence) tell whether it is still there. No
        # permessage-deflate: compressed video only costs HA CPU.
        browser = web.WebSocketResponse(max_msg_size=4096, compress=False)
        if not browser.can_prepare(request).ok:
            raise web.HTTPBadRequest()
        client = self.manager.client(entry_id)
        if client is None:
            raise web.HTTPNotFound()
        if len(self.manager.live_streams) >= MAX_LIVE_STREAMS:
            raise web.HTTPServiceUnavailable()
        stream = (entry_id, browser)
        self.manager.live_streams.add(stream)  # holds the slot while connecting
        upstream = None
        try:
            try:
                upstream, first = await client.open_live(camera_id, at=at)
            except SSError as err:
                self.manager.track(entry_id, err)
                _LOGGER.debug("Live stream of camera %s failed: %s", camera_id, err)
                raise web.HTTPBadGateway() from None
            self.manager.track(entry_id, None)
            if self.manager.client(entry_id) is not client:
                # The entry unloaded (or reloaded) while SS was connecting;
                # its client may have logged in again meanwhile.
                await client.logout()
                raise web.HTTPServiceUnavailable()
            await browser.prepare(request)
            await browser.send_bytes(first.data)
            await _relay(upstream, browser)
        finally:
            self.manager.live_streams.discard(stream)
            if upstream is not None:
                await upstream.close()
            if browser.prepared:
                await browser.close()
        return browser


# What the browser may tell SS's stream: jump (epoch seconds), pause, speed.
_STREAM_COMMAND = re.compile(r"time=[0-9]{9,11}|pause=(?:true|false)|speed=(?:0\.5|1|2|4|8|16)")


async def _relay(upstream: aiohttp.ClientWebSocketResponse, browser: web.WebSocketResponse) -> None:
    """Pass SS's messages on until either side goes.

    HA keeps SS's stream alive itself; the browser's messages (keep-alives,
    or the playback commands passed on) show that it's still there (a phone that dropped off Wi-Fi sends no reset, and
    the relay would otherwise hold the stream until TCP gives up).
    """

    async def down() -> None:
        async for msg in upstream:
            if msg.type != aiohttp.WSMsgType.BINARY:
                break
            # Awaits the browser's socket buffer: a slow viewer slows the
            # read from SS rather than piling data up in HA.
            await browser.send_bytes(msg.data)

    async def up() -> None:
        while True:
            # Background tabs run timers about once a minute: be generous.
            msg = await browser.receive(timeout=LIVE_IDLE_SECONDS)
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return
            if msg.type == aiohttp.WSMsgType.TEXT and _STREAM_COMMAND.fullmatch(msg.data):
                await upstream.send_str(msg.data)

    async def keep_alive() -> None:
        while True:
            await asyncio.sleep(LIVE_KEEP_ALIVE_SECONDS)
            await upstream.send_str("keepAlive")

    tasks = [asyncio.create_task(down()), asyncio.create_task(up()), asyncio.create_task(keep_alive())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# Every session has its own URLs, so a cached segment is never asked for again
# once its session is gone; storing it would only fill the client's disk cache
# (~2 GB per hour of a 4-camera grid). The player keeps what it needs in memory.
_NO_STORE = {"Cache-Control": "no-store"}


class _VodBaseView(HomeAssistantView):
    # The token in the path is the credential; see module docstring.
    requires_auth = False

    def __init__(self, manager: VodManager) -> None:
        self.manager = manager

    def _session(self, token: str) -> VodSession:
        session = self.manager.get_session(token)
        if session is None:
            if token in self.manager.superseded:
                raise web.HTTPGone()
            raise web.HTTPNotFound()
        return session

    @staticmethod
    def _segment(session: VodSession, index: str) -> Segment:
        i = int(index)
        if not 0 <= i < len(session.segments):
            raise web.HTTPNotFound()
        return session.segments[i]

    async def _get_parts(self, token: str, session: VodSession, seg: Segment) -> tuple[bytes, bytes]:
        try:
            return await self.manager.fetch(session, seg)
        except SSConnectionError:
            raise web.HTTPBadGateway() from None  # logged once by VodManager.track
        except SSError as err:
            if token in self.manager.superseded:
                raise web.HTTPGone() from None  # its queued segments were cancelled
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
        init, _ = await self._get_parts(token, session, self._segment(session, index))
        return web.Response(body=init, content_type="video/mp4", headers=_NO_STORE)


class VodSegmentView(_VodBaseView):
    url = VOD_URL + r"/{token}/seg/{index:\d+}.m4s"
    name = "api:surveillance_station:vod:segment"

    async def get(self, request: web.Request, token: str, index: str) -> web.Response:
        session = self._session(token)
        _, media = await self._get_parts(token, session, self._segment(session, index))
        return web.Response(body=media, content_type="video/iso.segment", headers=_NO_STORE)


class ThumbnailView(HomeAssistantView):
    """A frame of the recording at an event, for the card's event list.

    An ``<img>`` can't send an auth header, so the URL itself is the
    credential: the WebSocket hands out URLs signed by
    ``VodManager.sign_thumbnail``. Anything unsigned, expired or tampered with
    is a 404 (never a 401, which HA would count as a failed login).
    """

    requires_auth = False
    url = THUMBNAIL_URL + r"/{entry_id}/{camera_id:\d+}/{ts:\d+}.jpg"
    name = "api:surveillance_station:thumbnail"
    width = THUMBNAIL_WIDTH

    def __init__(self, manager: VodManager) -> None:
        self.manager = manager

    async def get(self, request: web.Request, entry_id: str, camera_id: str, ts: str) -> web.Response:
        if not self.manager.check_thumbnail(request.path, request.query.get("exp"), request.query.get("sig")):
            raise web.HTTPNotFound()
        try:
            jpg = await self.manager.thumbnail(entry_id, int(camera_id), int(ts), self.width)
        except SSConnectionError:
            raise web.HTTPBadGateway() from None
        except SSError as err:
            _LOGGER.debug("Thumbnail %s@%s failed: %s", camera_id, ts, err)
            raise web.HTTPBadGateway() from None
        if jpg is None:
            raise web.HTTPNotFound()
        # The URL names a fixed moment: the image never changes (and the URL
        # itself is good for at most two days).
        return web.Response(
            body=jpg, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=172800, immutable"}
        )


class LargeImageView(ThumbnailView):
    """The same frame, LARGE_IMAGE_WIDTH wide: the image in a notification."""

    url = THUMBNAIL_URL + r"/{entry_id}/{camera_id:\d+}/{ts:\d+}-large.jpg"
    name = "api:surveillance_station:image"
    width = LARGE_IMAGE_WIDTH
