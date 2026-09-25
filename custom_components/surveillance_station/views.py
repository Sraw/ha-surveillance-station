"""HTTP views: SS's stream relay, event thumbnails, and HLS VOD endpoints.

The card plays video from SS's own stream (LiveStreamView), live or recorded,
wherever the browser has MSE. The HLS endpoints serve browsers without it.

HLS flow: the card asks (over the authenticated WebSocket) for a playback window;
``VodManager.create_session`` plans the segments and returns an unguessable
token. The playlist and every segment URL live under that token, so the HTTP
views themselves need no HA auth header (a <video> cannot add one) - the token
*is* the capability, handed out only to authenticated WebSocket clients, and
it expires.

Each segment is fetched on demand (``synology_ss_playback.fetch_segment``:
``Recording.Download`` cuts the range out of the recording on the NAS, ffmpeg
stream-copies it into fragmented MP4). This module owns what is HA-specific:
the sessions, the segment cache and fetch queue, and the HTTP views.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections import OrderedDict
from dataclasses import dataclass, field
import logging
import re
import secrets
import time

import aiohttp
from aiohttp import web
from synology_ss_playback import (
    Bookmark,
    Segment,
    SSConnectionError,
    SSError,
    SurveillanceStationClient,
    fetch_segment,
    fetch_snapshot,
    live_edge,
    plan_segments,
    recordings_from,
    render_playlist,
)

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import (
    BOOKMARK_CACHE_SECONDS,
    BOOKMARK_FRAMES_MAX,
    BOOKMARK_ERROR_SECONDS,
    DOMAIN,
    LIVE_IDLE_SECONDS,
    LIVE_TOKEN_TTL_SECONDS,
    LIVE_URL,
    MAX_LIVE_STREAMS,
    MAX_PARALLEL_FETCHES,
    MAX_PARALLEL_THUMBNAILS,
    SEGMENT_CACHE_BYTES,
    THUMBNAIL_CACHE_BYTES,
    THUMBNAIL_DISK_BYTES,
    THUMBNAIL_ENTRY_BYTES,
    THUMBNAIL_MISS_SECONDS,
    THUMBNAIL_URL,
    THUMBNAIL_URL_TTL_HOURS,
    VOD_MAX_SESSIONS,
    VOD_SESSION_TTL_SECONDS,
    VOD_URL,
)
from .thumbnail_store import ThumbnailStore

_LOGGER = logging.getLogger(__name__)

# How often thumbnail_when_recorded asks SS whether the moment is written yet.
THUMBNAIL_POLL_SECONDS = 2


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
        # (entry_id, camera_id, ts) -> (made at, JPEG or b"" for "nothing
        # recorded then"), the jobs making them and how many requests wait on each.
        self._thumbs: OrderedDict[tuple[str, int, int], tuple[float, bytes]] = OrderedDict()
        self._thumbs_bytes = 0
        self._thumb_tasks: dict[tuple[str, int, int], asyncio.Task] = {}
        self._thumb_waiters: dict[tuple[str, int, int], int] = {}
        self._thumb_sem = asyncio.Semaphore(MAX_PARALLEL_THUMBNAILS)
        self.disk = ThumbnailStore(hass, hass.config.cache_path(DOMAIN, "thumbnails"), THUMBNAIL_DISK_BYTES)
        # Thumbnail URLs carry an HMAC under this key (see sign_thumbnail),
        # kept across restarts (async_load) so the URLs, and the browser's
        # cached copies, stay good.
        self._thumb_key = secrets.token_bytes(32)
        self._key_store: Store[dict[str, str]] = Store(hass, 1, f"{DOMAIN}.thumbnail_key", private=True)
        # "entry_id/bookmark_id" -> the moment its thumbnail shows (see set_frame), oldest first.
        self._frames: OrderedDict[str, int] = OrderedDict()
        self._frame_store: Store[dict[str, int]] = Store(hass, 1, f"{DOMAIN}.bookmark_frames")
        # entry_id -> (fetched at, every bookmark newest first, or the error),
        # and the one fetch per entry that every request waits on.
        self._bookmarks: dict[str, tuple[float, list[Bookmark] | SSError]] = {}
        self._bookmark_tasks: dict[str, asyncio.Task] = {}
        # Live streams: unused tokens -> (entry_id, camera_id, expires), and
        # the relays running (entry_id, browser socket).
        self._live_tokens: dict[str, tuple[str, int, float | None, float]] = {}
        self.live_streams: set[tuple[str, web.WebSocketResponse]] = set()

    async def async_load(self) -> None:
        """Load the thumbnail signing key (made on first use) and the disk cache's index."""
        data = await self._key_store.async_load()
        try:
            key = bytes.fromhex(data["key"])  # type: ignore[index]
        except (TypeError, KeyError, ValueError):
            key = b""
        if len(key) == 32:
            self._thumb_key = key
        else:  # none yet, or not one of ours
            await self._key_store.async_save({"key": self._thumb_key.hex()})
        await self.disk.load()
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
        for tkey in [k for k in self._thumbs if k[0] == entry_id]:
            self._drop_thumbnail(tkey)
        for tasks in (self._inflight, self._thumb_tasks):
            for key, task in list(tasks.items()):
                if key[0] == entry_id:
                    task.cancel()
                    del tasks[key]
        if (task := self._bookmark_tasks.pop(entry_id, None)) is not None:
            task.cancel()
        for token in [t for t, v in self._live_tokens.items() if v[0] == entry_id]:
            del self._live_tokens[token]
        for stream in [x for x in self.live_streams if x[0] == entry_id]:
            self.hass.async_create_task(stream[1].close(code=aiohttp.WSCloseCode.GOING_AWAY))
        self._unreachable.discard(entry_id)
        self._bookmarks.pop(entry_id, None)

    def stats(self) -> dict[str, int]:
        return {
            "sessions": len(self._sessions),
            "cached_segments": len(self._cache),
            "cached_bytes": self._cache_bytes,
            "fetches_in_flight": len(self._inflight),
            "cached_thumbnails": len(self._thumbs),
            "cached_thumbnail_bytes": self._thumbs_bytes,
            "disk_thumbnails": len(self.disk),
            "disk_thumbnail_bytes": self.disk.bytes,
            "live_streams": len(self.live_streams),
        }

    async def bookmarks(self, entry_id: str, client: SurveillanceStationClient) -> list[Bookmark]:
        """Every bookmark of every camera, newest first, at most a few seconds old.

        One fetch per entry at a time, whose result (or error) every request
        waiting meanwhile shares: a NAS that hangs costs one timeout, not one
        per request queued behind it.
        """
        if (hit := self._bookmarks.get(entry_id)) is not None:
            at, found = hit
            if isinstance(found, SSError):
                if time.monotonic() - at < BOOKMARK_ERROR_SECONDS:
                    raise found.with_traceback(None)
            elif time.monotonic() - at < BOOKMARK_CACHE_SECONDS:
                return found
        task = self._bookmark_tasks.get(entry_id)
        if task is None:
            task = self.hass.async_create_background_task(
                self._load_bookmarks(entry_id, client), "surveillance_station bookmarks", eager_start=False
            )
            task.add_done_callback(_retrieve_exception)
            self._bookmark_tasks[entry_id] = task
        return await _join(task, "bookmarks")

    async def _load_bookmarks(self, entry_id: str, client: SurveillanceStationClient) -> list[Bookmark]:
        me = asyncio.current_task()
        try:
            cameras = await client.cameras()
            found = await client.list_bookmarks([c.id for c in cameras])
        except SSError as err:
            self._bookmarks[entry_id] = (time.monotonic(), err)
            raise
        finally:
            if self._bookmark_tasks.get(entry_id) is me:
                del self._bookmark_tasks[entry_id]
        self._bookmarks[entry_id] = (time.monotonic(), found)
        return found

    def forget_bookmarks(self, entry_id: str) -> None:
        """Changed in SS by us: list them afresh on the next request."""
        self._bookmarks.pop(entry_id, None)

    async def thumbnail_when_recorded(self, entry_id: str, camera_id: int, ts: int, wait: float) -> bytes | None:
        """The frame at ts once SS has written it, waiting at most wait seconds.

        SS moves a recording's reported end forward every ~10 s, to what it
        has written: until that passes ts, a cut there could come out short
        (an earlier keyframe) or empty, and that would be kept.
        """
        deadline = time.monotonic() + wait
        while True:
            client = self.client(entry_id)
            try:
                if client is not None and any(
                    r.start <= ts and r.end >= ts + 1 for r in await client.recordings(camera_id, ts - 1, ts + 1)
                ):
                    return await self.thumbnail(entry_id, camera_id, ts)
            except SSError:
                pass
            if time.monotonic() + THUMBNAIL_POLL_SECONDS > deadline:
                return None
            await asyncio.sleep(THUMBNAIL_POLL_SECONDS)

    def sign_thumbnail(self, entry_id: str, camera_id: int, ts: int) -> str:
        """A URL for the frame of camera_id at ts, valid for one to two days.

        An HMAC of our own rather than HA's async_sign_path: HA answers an
        expired or foreign signature (after every HA restart, as its signing
        key lives in memory) with 401, and counts each 401 as a failed login,
        so a long-open card would get its device IP-banned. A bad signature
        here is a plain 404. The expiry is the end of the (UTC) day plus a
        day, so the URL (and the browser's cached copy) is the same all day,
        across list refreshes and HA restarts.
        """
        exp = (int(time.time()) // 86400 + 1) * 86400 + THUMBNAIL_URL_TTL_HOURS * 3600
        path = f"{THUMBNAIL_URL}/{entry_id}/{camera_id}/{ts}.jpg"
        return f"{path}?exp={exp}&sig={self._thumbnail_sig(path, exp)}"

    def check_thumbnail(self, path: str, exp: str | None, sig: str | None) -> bool:
        # isascii: str.isdigit() accepts "²", and compare_digest rejects non-ASCII str.
        if not exp or not sig or not (exp.isascii() and exp.isdigit()) or not sig.isascii():
            return False
        if int(exp) < time.time():
            return False
        return hmac.compare_digest(sig, self._thumbnail_sig(path, int(exp)))

    def _thumbnail_sig(self, path: str, exp: int) -> str:
        return hmac.new(self._thumb_key, f"{path}\n{exp}".encode(), hashlib.sha256).hexdigest()[:32]

    async def thumbnail(self, entry_id: str, camera_id: int, ts: int) -> bytes | None:
        """JPEG of camera_id at ts, or None if nothing was recorded then.

        Requests for the same frame share one job; a job nobody waits for any
        more (the thumbnail was scrolled past) is cancelled.
        """
        key = (entry_id, camera_id, ts)
        if (hit := self._thumbs.get(key)) is not None:
            at, data = hit
            # A miss is only remembered briefly: the recording may just not
            # have been listed yet.
            if data or time.monotonic() - at < THUMBNAIL_MISS_SECONDS:
                self._thumbs.move_to_end(key)
                return data or None
            self._drop_thumbnail(key)
        task = self._thumb_tasks.get(key)
        if task is None or task.cancelling():
            task = self.hass.async_create_background_task(
                self._make_thumbnail(key), f"surveillance_station thumbnail {camera_id}@{ts}", eager_start=False
            )
            task.add_done_callback(_retrieve_exception)
            self._thumb_tasks[key] = task
        self._thumb_waiters[key] = self._thumb_waiters.get(key, 0) + 1
        try:
            return await _join(task, "thumbnail")
        finally:
            if left := self._thumb_waiters.pop(key) - 1:
                self._thumb_waiters[key] = left
            elif not task.done():
                task.cancel()

    async def _make_thumbnail(self, key: tuple[str, int, int]) -> bytes | None:
        me = asyncio.current_task()
        entry_id, camera_id, ts = key
        try:
            # Made before (even before a restart): no need for the NAS.
            if (jpg := await self.disk.get(key)) is None:
                client = self.client(entry_id)
                if client is None:
                    raise SSError("thumbnail", "fetch", None, "Surveillance Station entry is not loaded")
                async with self._thumb_sem:
                    try:
                        jpg = await fetch_snapshot(client, camera_id, ts, get_ffmpeg_manager(self.hass).binary)
                    except SSError as err:
                        self.track(entry_id, err)
                        raise
                self.track(entry_id, None)
                if jpg:  # "nothing recorded" is only remembered in memory, briefly
                    self.disk.put(key, jpg)
        finally:
            if self._thumb_tasks.get(key) is me:
                del self._thumb_tasks[key]
        self._drop_thumbnail(key)
        data = jpg or b""
        self._thumbs[key] = (time.monotonic(), data)
        self._thumbs_bytes += len(data) + THUMBNAIL_ENTRY_BYTES
        while self._thumbs_bytes > THUMBNAIL_CACHE_BYTES and len(self._thumbs) > 1:
            self._drop_thumbnail(next(iter(self._thumbs)))
        return data or None

    def _drop_thumbnail(self, key: tuple[str, int, int]) -> None:
        if (old := self._thumbs.pop(key, None)) is not None:
            self._thumbs_bytes -= len(old[1]) + THUMBNAIL_ENTRY_BYTES

    def create_live_token(self, entry_id: str, camera_id: int, at: float | None = None) -> str:
        """A single-use token for a camera's stream: real time, or recordings from ``at``."""
        now = time.time()
        for token in [t for t, v in self._live_tokens.items() if v[3] < now]:
            del self._live_tokens[token]
        token = secrets.token_urlsafe(32)
        self._live_tokens[token] = (entry_id, camera_id, at, now + LIVE_TOKEN_TTL_SECONDS)
        return token

    def take_live_token(self, token: str) -> tuple[str, int, float | None] | None:
        """(entry_id, camera_id, at) of an unused, unexpired token; it is used up."""
        found = self._live_tokens.pop(token, None)
        if found is None or found[3] < time.time():
            return None
        return found[:3]

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
            # The fetch runs as its own task so a client abort (a player may
            # abort on every seek) neither kills the work nor the other requests
            # waiting for the same segment; a retry then finds it cached.
            # Not eager: a task that finished inside the call (an error before
            # the first await) would clear its _inflight slot before it was
            # set, and the finished task would then answer for that segment
            # for good.
            task = self.hass.async_create_background_task(
                self._fetch_and_cache(key, session.entry_id, seg),
                f"surveillance_station segment {seg.recording_id}@{seg.offset_ms}",
                eager_start=False,
            )
            task.add_done_callback(_retrieve_exception)
            self._inflight[key] = task
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            return await _join(task, "vod")
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
        # keep-alives (or their absence) tell whether it is still there.
        browser = web.WebSocketResponse(max_msg_size=4096)
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
            await asyncio.sleep(10)
            await upstream.send_str("keepAlive")

    tasks = [asyncio.create_task(down()), asyncio.create_task(up()), asyncio.create_task(keep_alive())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _join[T](task: asyncio.Task[T], what: str) -> T:
    """Wait for a shared job; its cancellation (entry unloaded) is an error, not ours.

    A CancelledError escaping into a WebSocket handler would leave the card's
    call unanswered for good; a view would drop the connection.
    """
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if (me := asyncio.current_task()) is not None and me.cancelling():
            raise  # the request itself was cancelled
        raise SSError(what, "fetch", None, "Surveillance Station entry was unloaded") from None


DATA_MANAGER: HassKey[VodManager] = HassKey(DOMAIN)


def _retrieve_exception(task: asyncio.Task) -> None:
    # Everyone waiting may have gone away; don't log "never retrieved".
    if not task.cancelled():
        task.exception()


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

    def __init__(self, manager: VodManager) -> None:
        self.manager = manager

    async def get(self, request: web.Request, entry_id: str, camera_id: str, ts: str) -> web.Response:
        if not self.manager.check_thumbnail(request.path, request.query.get("exp"), request.query.get("sig")):
            raise web.HTTPNotFound()
        try:
            jpg = await self.manager.thumbnail(entry_id, int(camera_id), int(ts))
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
