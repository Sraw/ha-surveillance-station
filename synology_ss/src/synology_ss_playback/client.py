"""Minimal async client for the Synology Surveillance Station Web API.

Only what playback needs: login, cameras, recordings (with start/stop times),
bookmarks (listed, and created/edited/deleted for detections from elsewhere),
time-lapse files, and cutting a time range out of a recording.

API notes (verified against SS 9.x, see the repo README):
* ``SYNO.SurveillanceStation.Event`` ``List`` v5 is the call that returns
  ``startTime``/``stopTime`` per recording; ``Recording.List`` v6 does not.
* Both ``Event.List`` and ``Recording.List`` filter ``fromTime``/``toTime`` on
  a recording's *start* time, not on overlap, so a query must reach back by
  the longest recording length (``RECORDING_LOOKBACK_SECONDS``) and then
  filter by overlap itself.
* ``Recording.Download`` v6 with ``offsetTimeMs``/``playTimeMs`` returns an
  MP4 (moov at the end, no Range support). On failure it returns JSON.
* Bookmarks: the documented ``ThirdParty.Bookmark.List`` v1 (SS 9.3 API
  reference) returns every bookmark of the given cameras, newest first, with
  times as NAS-local ISO strings without an offset (converted here with SS
  Info's ``timezoneTZDB``). Its ``startTime``/``endTime`` filters only work
  at day granularity and misplace the boundaries, so they are not used: the
  caller filters the full list.
* ``ThirdParty.Bookmark.Create``/``Edit`` take ``startTime``/``endTime`` as
  epoch seconds and are exact to the second. A NAS-local time without an
  offset is read an hour late in summer time (the same bug as SnapShot);
  one with an offset, or unquoted, is taken as 1970.
* Time-lapse (undocumented; what SS's own UI calls, verified on SS 9.x):
  ``SYNO.SurveillanceStation.TimeLapse.Recording`` ``List`` v1 lists the
  files (``lapseId`` -1 = every task). ``Recording.Download`` v6 cuts them
  with ``recEvtType=3``; offsets are in the file's video time, and the cut
  starts exactly on the frame asked for (every frame is a keyframe) and
  holds the whole seconds asked for plus 29 frames. A file still being written
  can be cut too, but SS reads it about a third as fast.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Error codes that mean "log in again and retry" (common DSM Web API codes).
SESSION_ERRORS = {105, 106, 107, 119}
# Not 407 ("IP blocked"): DSM's auto-block expires, the password is fine;
# but no login is tried for BLOCKED_SECONDS after it.
AUTH_FAILED_ERRORS = {400, 401, 402, 403, 404, 406, 408, 409, 410}
BLOCKED_ERROR = 407
BLOCKED_SECONDS = 60
# After refused credentials, one login is let through this often: often
# enough to recover from a refusal that was not about the password, far
# below DSM's auto-block threshold (10 failures in 5 minutes by default).
AUTH_RETRY_SECONDS = 1800
# DSM's codes for "this account needs a two-step verification code".
OTP_ERRORS = {403, 404, 406}
# The Web APIs (and the versions) this client calls: an older Surveillance
# Station lacks some, see missing_apis().
REQUIRED_APIS = {
    "SYNO.API.Auth": 6,
    "SYNO.SurveillanceStation.Info": 8,
    "SYNO.SurveillanceStation.Camera": 9,
    "SYNO.SurveillanceStation.Event": 5,
    "SYNO.SurveillanceStation.Recording": 6,
    "SYNO.SurveillanceStation.ThirdParty.Bookmark": 1,
}
VIDEO_CODEC_H265 = 6
# Recording.Download's recEvtType for a time-lapse file (SYNO.SS.Event.RecType.TIMELAPSE).
REC_EVT_TIMELAPSE = 3
TIMELAPSE_PAGE = 100  # the most TimeLapse.Recording.List returns at once
DOWNLOAD_CHUNK = 1 << 20
DOWNLOAD_ERROR_MAX = 64 * 1024  # of a JSON error reply read
# SS splits continuous recordings into files of at most this length (the
# per-camera setting tops out well below it).
RECORDING_LOOKBACK_SECONDS = 4 * 3600
# A freshly opened live socket that sends nothing at all (SS wedged) within
# this long is given up on rather than held forever.
LIVE_CONNECT_TIMEOUT_SECONDS = 15


# What a malformed list entry raises while being read.
_BAD_ITEM = (KeyError, TypeError, ValueError, AttributeError, OverflowError)

# Patchable in tests (time.monotonic itself is the event loop's clock).
_monotonic = time.monotonic


def _items(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """data[key] as a list of objects: anything else in it is left out."""
    value = data.get(key)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _error_code(data: dict[str, Any]) -> Any:
    error = data.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _is_hevc(codec: Any) -> bool:
    """SS reports videoCodec as an int (6 = H.265) or, in some calls, a name."""
    if isinstance(codec, int):
        return codec == VIDEO_CODEC_H265
    return str(codec or "").upper() in ("H265", "HEVC")


# A refused live stream re-logs in at most this often (see open_live).
LIVE_RELOGIN_SECONDS = 60

class SSError(Exception):
    """A Surveillance Station API call failed."""

    def __init__(self, api: str, method: str, code: int | None, detail: Any = None) -> None:
        super().__init__(f"{api}.{method} failed: code={code} {detail or ''}".strip())
        self.api = api
        self.method = method
        self.code = code


class SSAuthError(SSError):
    """Credentials were rejected."""


class SSConnectionError(SSError):
    """Surveillance Station could not be reached."""


@dataclass(frozen=True)
class SSInfo:
    """Identity of the Surveillance Station host."""

    serial: str  # the NAS serial number: stable across IP / port changes
    hostname: str
    version: str
    timezone: str  # TZDB name, e.g. "US/Pacific"; SS reports local times in it


@dataclass(frozen=True)
class Camera:
    id: int
    name: str
    enabled: bool


@dataclass(frozen=True)
class RecordingInfo:
    id: int
    camera_id: int
    start: int
    end: int
    mount_id: int
    live: bool
    hevc: bool


@dataclass(frozen=True)
class TimelapseRecording:
    """One file of a Surveillance Station time-lapse task.

    A task writes a video of at most its "truncate length" (6 min by default,
    at TIMELAPSE_FPS) and then starts the next file; at the task's compression
    (240x by default) that's a day of wall time. Slowed-down stretches (SS's
    own events) fill it faster, so a file can cover less than that.
    """

    id: int
    camera_id: int
    task_id: int
    start: int  # wall time of the first frame
    span: int  # wall seconds the file covers so far (SS's rangeMinute, whole minutes)
    frames: int
    width: int
    height: int
    hevc: bool
    live: bool  # still being written


@dataclass(frozen=True)
class Bookmark:
    id: int
    camera_id: int
    name: str
    comment: str
    start: int
    end: int


class SurveillanceStationClient:
    """One logged-in session against a Surveillance Station host."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        use_https: bool,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # an IPv6 literal
        self._base = f"{'https' if use_https else 'http'}://{host}:{port}/webapi"
        self.nas = f"{host}:{port}"  # which NAS, for per-NAS caches
        self._username = username
        self._password = password
        self._sid: str | None = None
        self._login_lock = asyncio.Lock()
        self._auth_failed: int | None = None
        self._auth_failed_at = 0.0
        self._blocked_until = -1.0
        self._tz: ZoneInfo | None = None
        self._live_relogin_at = -LIVE_RELOGIN_SECONDS
        # Called when a re-login is refused at runtime (password changed).
        self.on_auth_failed: Callable[[], None] | None = None

    async def login(self, stale_sid: str | None = None) -> None:
        """Get a session unless another caller already replaced ``stale_sid``.

        Concurrent callers that all saw the same (missing or expired) sid end
        up with one login between them.
        """
        async with self._login_lock:
            if self._auth_failed is not None and _monotonic() - self._auth_failed_at < AUTH_RETRY_SECONDS:
                # Credentials were refused; retrying on every call would trip
                # DSM's auto-block for this host within minutes. A successful
                # reauth reloads the entry with a new client.
                raise SSAuthError("SYNO.API.Auth", "login", self._auth_failed)
            if self._sid is not None and self._sid != stale_sid:
                return
            if _monotonic() < self._blocked_until:
                raise SSError("SYNO.API.Auth", "login", BLOCKED_ERROR, "this host is blocked by DSM for now")
            # POST so the password never sits in a URL (URLs end up in
            # exception text and logs).
            data = await self._raw_json(
                "auth.cgi",
                {
                    "api": "SYNO.API.Auth",
                    "method": "login",
                    "version": 6,
                    "account": self._username,
                    "passwd": self._password,
                    "session": "SurveillanceStation",
                    "format": "sid",
                },
                post=True,
            )
            if not data.get("success"):
                code = _error_code(data)
                if code in AUTH_FAILED_ERRORS:
                    self._auth_failed = code
                    self._auth_failed_at = _monotonic()
                    self._sid = None
                    if self.on_auth_failed is not None:
                        self.on_auth_failed()
                    raise SSAuthError("SYNO.API.Auth", "login", code)
                if code == BLOCKED_ERROR:
                    self._blocked_until = _monotonic() + BLOCKED_SECONDS
                raise SSError("SYNO.API.Auth", "login", code)
            result = data.get("data")
            sid = result.get("sid") if isinstance(result, dict) else None
            if not isinstance(sid, str) or not sid:
                raise SSError("SYNO.API.Auth", "login", None, "answer without a session")
            self._auth_failed = None
            self._sid = sid

    async def logout(self) -> None:
        if self._sid is None:
            return
        try:
            # Short: an unload (and so a reload) waits for it, and a NAS that
            # is gone would hold it for the full request timeout.
            await self._raw_json(
                "auth.cgi",
                {"api": "SYNO.API.Auth", "method": "logout", "version": 6,
                 "session": "SurveillanceStation", "_sid": self._sid},
                timeout=5,
            )
        except SSError:
            pass
        self._sid = None

    async def open_live(
        self, camera_id: int, heartbeat: float = 30, at: float | None = None
    ) -> tuple[aiohttp.ClientWebSocketResponse, aiohttp.WSMessage]:
        """Connect to a camera's stream; returns the socket and its first message.

        Real time, or with ``at`` (epoch seconds) the recordings from then on:
        SS starts at the keyframe before ``at``, skips ahead over gaps to the
        next recording, and paces the frames in real time (``speed=N`` and
        ``pause=true|false`` messages change that; ``time=<epoch>`` jumps).
        The time goes as epoch seconds: a bare local time is read an hour off
        during DST.

        The documented WebSocket stream (``/ss_webstream_task/``, see the SS
        Web API "Liveview / Playback" page): binary messages of a 4-byte
        big-endian header end offset, a query-string header (``vdoCodec`` /
        ``adoCodec`` first, then ``mediaType`` 1 = video / 2 = audio, ``key``,
        ``msec``), and fragmented MP4 (``ftyp``, ``moov``, then ``moof`` +
        ``mdat`` per frame). The client sends ``keepAlive`` every 10 s.

        SS answers an unknown or expired sid by closing at once, so a close
        before any data means: log in again and retry, once. The URL carries
        the sid and never appears in errors.
        """
        base = self._base.removesuffix("/webapi").replace("http", "ws", 1)
        for attempt in (1, 2):
            if self._sid is None:
                await self.login()
            sid = self._sid
            try:
                ws = await self._session.ws_connect(
                    f"{base}/ss_webstream_task/?camId={int(camera_id)}&_sid={sid}"
                    + ("" if at is None else f"&time={int(at)}"),
                    heartbeat=heartbeat,
                    max_msg_size=0,
                    timeout=aiohttp.ClientWSTimeout(ws_close=5),
                )
            except (aiohttp.ClientError, TimeoutError) as err:
                raise SSConnectionError("ss_webstream_task", "connect", None, type(err).__name__) from None
            try:
                first = await asyncio.wait_for(ws.receive(), LIVE_CONNECT_TIMEOUT_SECONDS)
            except TimeoutError:
                await ws.close()
                raise SSConnectionError("ss_webstream_task", "receive", None, "no data") from None
            except BaseException:
                await ws.close()
                raise
            if first.type == aiohttp.WSMsgType.BINARY:
                return ws, first
            await ws.close()
            # Closed before any data: an expired sid, or a camera SS won't
            # stream (offline, disabled). Log in again at most once a minute,
            # so a camera that stays refused doesn't mean a DSM login per try.
            now = _monotonic()
            if attempt == 2 or now - self._live_relogin_at < LIVE_RELOGIN_SECONDS:
                break
            self._live_relogin_at = now
            await self.login(stale_sid=sid)
        raise SSError("ss_webstream_task", "connect", None, f"stream of camera {camera_id} refused")

    async def _request(self, path: str, params: dict[str, Any], post: bool, timeout: float) -> tuple[bytes, str]:
        """One HTTP round trip; errors never carry the URL (it holds the sid)."""
        url = f"{self._base}/{path}"
        kwargs = {"data": params} if post else {"params": params}
        api, method = params.get("api", path), params.get("method", "")
        try:
            async with self._session.request(
                "POST" if post else "GET", url, timeout=aiohttp.ClientTimeout(total=timeout), **kwargs
            ) as resp:
                if resp.status >= 400:
                    raise SSError(api, method, None, f"HTTP {resp.status}")
                return await resp.read(), resp.headers.get("Content-Type", "")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SSConnectionError(api, method, None, type(err).__name__) from None

    async def _raw_json(
        self, path: str, params: dict[str, Any], post: bool = False, timeout: float = 30
    ) -> dict[str, Any]:
        body, _ = await self._request(path, params, post, timeout)
        api, method = params.get("api", path), params.get("method", "")
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise SSError(api, method, None, "non-JSON reply") from None
        # Anything but a JSON object (a list, null, a proxy's page) is an
        # SSError too, never an AttributeError further on: callers (setup)
        # retry SSErrors and treat anything else as a bug.
        if not isinstance(data, dict):
            raise SSError(api, method, None, "unexpected reply")
        return data

    async def _call(self, api: str, method: str, version: int, **params: Any) -> dict[str, Any]:
        """entry.cgi call with one transparent re-login on session errors."""
        for attempt in (1, 2):
            if self._sid is None:
                await self.login(stale_sid=None)
            sid = self._sid
            data = await self._raw_json(
                "entry.cgi",
                {"api": api, "method": method, "version": version, "_sid": sid, **params},
            )
            if data.get("success"):
                result = data.get("data") or {}
                if not isinstance(result, dict):
                    raise SSError(api, method, None, "unexpected reply")
                return result
            code = _error_code(data)
            if code in SESSION_ERRORS and attempt == 1:
                _LOGGER.debug("Session error %s on %s.%s, logging in again", code, api, method)
                await self.login(stale_sid=sid)
                continue
            raise SSError(api, method, code, data.get("error"))
        raise AssertionError("unreachable")  # pragma: no cover

    async def missing_apis(self) -> list[str]:
        """The Web APIs this client needs that the NAS lacks, or has only in older versions (no login needed)."""
        data = await self._raw_json(
            "query.cgi", {"api": "SYNO.API.Info", "method": "query", "version": 1, "query": ",".join(REQUIRED_APIS)}
        )
        apis = data.get("data") if data.get("success") else None
        if not isinstance(apis, dict):
            raise SSError("SYNO.API.Info", "query", _error_code(data), "unexpected reply")
        if not any(api.startswith("SYNO.SurveillanceStation.") for api in apis):
            # None at all: the package is stopped (or updating), not old.
            raise SSConnectionError("SYNO.API.Info", "query", None, "Surveillance Station isn't running")
        missing = []
        for api, version in REQUIRED_APIS.items():
            try:
                ok = int(apis[api]["minVersion"]) <= version <= int(apis[api]["maxVersion"])
            except _BAD_ITEM:
                ok = False
            if not ok:
                missing.append(f"{api} v{version}")
        return missing

    async def info(self) -> SSInfo:
        data = await self._call("SYNO.SurveillanceStation.Info", "GetInfo", 8)
        if not data.get("serial"):
            # Seen once (2026-09-25) right after an HA restart: a success
            # answer without the serial, fine again moments later. An SSError
            # makes setup retry rather than fail for good.
            raise SSError("SYNO.SurveillanceStation.Info", "GetInfo", None, "answer without a serial")
        v = data.get("version") or {}
        version = ".".join(str(v[k]) for k in ("major", "minor", "small") if k in v)
        if "build" in v:
            version += f"-{v['build']}"
        info = SSInfo(
            serial=str(data["serial"]),
            hostname=str(data.get("hostname", "")),
            version=version,
            timezone=str(data.get("timezoneTZDB") or "UTC"),
        )
        if not data.get("timezoneTZDB"):
            # Could be the same kind of partial answer, so UTC is not cached:
            # the next _timezone() asks again.
            _LOGGER.warning("Surveillance Station reports no time zone; reading its local times as UTC")
            self._tz = None
            return info
        try:
            self._tz = ZoneInfo(info.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            _LOGGER.warning("Unknown NAS time zone %r, assuming UTC", info.timezone)
            self._tz = ZoneInfo("UTC")
        return info

    async def timezone(self) -> ZoneInfo:
        """The NAS's time zone (SS reports local times in it)."""
        return await self._timezone()

    async def _timezone(self) -> ZoneInfo:
        if self._tz is None:
            await self.info()
        return self._tz or ZoneInfo("UTC")

    async def cameras(self) -> list[Camera]:
        data = await self._call("SYNO.SurveillanceStation.Camera", "List", 9)
        out = []
        for c in _items(data, "cameras"):
            try:
                out.append(
                    Camera(
                        id=int(c["id"]),
                        name=str(c.get("newName") or c.get("name") or c["id"]),
                        enabled=bool(c.get("enabled", True)),
                    )
                )
            except _BAD_ITEM:
                _LOGGER.debug("Skipping a camera SS lists as %r", c)
        return out

    async def recordings(self, camera_id: int, start: int, end: int) -> list[RecordingInfo]:
        """Recordings of one camera that overlap [start, end], oldest first."""
        out: list[RecordingInfo] = []
        offset = 0
        while True:
            data = await self._call(
                "SYNO.SurveillanceStation.Event", "List", 5,
                cameraIds=str(camera_id), fromTime=int(start) - RECORDING_LOOKBACK_SECONDS,
                toTime=int(end), offset=offset, limit=200,
            )
            events = _items(data, "events")
            for e in events:
                try:
                    if e.get("deleted") or e.get("markAsDel"):
                        continue
                    if int(e["stopTime"]) < start and not e.get("recording"):
                        continue
                    out.append(
                        RecordingInfo(
                            id=int(e["id"]),
                            camera_id=int(e["cameraId"]),
                            start=int(e["startTime"]),
                            end=int(e["stopTime"]),
                            mount_id=int(e.get("mountId") or 0),
                            live=bool(e.get("recording")),
                            hevc=_is_hevc(e.get("videoCodec")),
                        )
                    )
                except _BAD_ITEM:
                    _LOGGER.debug("Skipping a recording SS lists as %r", e)
            offset += len(events)
            try:
                total = int(data.get("total", 0))
            except _BAD_ITEM:
                total = 0
            if not events or offset >= total:
                break
        out.sort(key=lambda r: r.start)
        return out

    async def timelapse_recordings(self) -> list[TimelapseRecording]:
        """Every time-lapse file of every task, oldest first.

        ``SYNO.SurveillanceStation.TimeLapse.Recording`` is undocumented (what
        SS's own UI calls). Its fromTime/toTime filter only matches a file's
        start, so everything is listed and callers pick; a camera keeps one
        file per day of its task's retention.
        """
        out: list[TimelapseRecording] = []
        offset = 0
        while True:
            data = await self._call(
                "SYNO.SurveillanceStation.TimeLapse.Recording", "List", 1,
                lapseId=-1, start=offset, limit=TIMELAPSE_PAGE,
            )
            files = _items(data, "events")
            for e in files:
                try:
                    if e.get("markAsDel"):
                        continue
                    frames = int(e["frameCount"])
                    if frames <= 0:
                        continue
                    out.append(
                        TimelapseRecording(
                            id=int(e["id"]),
                            camera_id=int(e["cameraId"]),
                            task_id=int(e.get("taskId") or 0),
                            start=int(e["startTime"]),
                            span=int(e["rangeMinute"]) * 60,
                            frames=frames,
                            width=int(e.get("imgWidth") or 0),
                            height=int(e.get("imgHeight") or 0),
                            hevc=_is_hevc(e.get("video_type")),
                            live=bool(e.get("recording")),
                        )
                    )
                except _BAD_ITEM:
                    _LOGGER.debug("Skipping a time-lapse file SS lists as %r", e)
            offset += len(files)
            try:
                total = int(data.get("total", 0))
            except _BAD_ITEM:
                total = 0
            if not files or offset >= total:
                break
        out.sort(key=lambda r: r.start)
        return out

    async def list_bookmarks(self, camera_ids: list[int]) -> list[Bookmark]:
        """Every bookmark of these cameras, newest first."""
        if not camera_ids:
            return []
        tz = await self._timezone()
        data = await self._call(
            "SYNO.SurveillanceStation.ThirdParty.Bookmark", "List", 1,
            camIds=",".join(str(int(c)) for c in camera_ids),
        )
        out = []
        for b in _items(data, "bookmarks"):
            # One odd entry must not hide all the others.
            try:
                out.append(self._parse_bookmark(b, int(b["camId"]), tz))
            except _BAD_ITEM:
                _LOGGER.debug("Skipping a bookmark SS lists as %r", b)
        out.sort(key=lambda b: (b.start, b.id), reverse=True)
        return out

    async def create_bookmark(
        self, camera_id: int, name: str, start: float, end: float, comment: str = ""
    ) -> Bookmark:
        """A bookmark on a camera's timeline from start to end (epoch seconds)."""
        data = await self._call(
            "SYNO.SurveillanceStation.ThirdParty.Bookmark", "Create", 1,
            camId=int(camera_id), name=_quoted(name), comment=_quoted(comment),
            startTime=int(start), endTime=int(max(end, start)),
        )
        return self._bookmark(data, camera_id, await self._timezone(), "Create")

    async def edit_bookmark(
        self, bookmark_id: int, camera_id: int, name: str, start: float, end: float, comment: str = ""
    ) -> Bookmark:
        """Replace a bookmark's name, comment and times (all of them)."""
        data = await self._call(
            "SYNO.SurveillanceStation.ThirdParty.Bookmark", "Edit", 1,
            bookmarkId=int(bookmark_id), name=_quoted(name), comment=_quoted(comment),
            startTime=int(start), endTime=int(max(end, start)),
        )
        return self._bookmark(data, camera_id, await self._timezone(), "Edit")

    async def delete_bookmarks(self, bookmark_ids: list[int]) -> None:
        if bookmark_ids:
            await self._call(
                "SYNO.SurveillanceStation.ThirdParty.Bookmark", "Delete", 1,
                bookmarkIds=",".join(str(int(i)) for i in bookmark_ids),
            )

    @staticmethod
    def _bookmark(data: dict[str, Any], camera_id: int, tz: ZoneInfo, method: str) -> Bookmark:
        # Create/Edit answer with the bookmark as SS stored it (local times).
        items = _items(data, "bookmark")
        try:
            return SurveillanceStationClient._parse_bookmark(items[0], camera_id, tz)
        except (*_BAD_ITEM, IndexError):
            raise SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", method, None, str(data)[:200]) from None

    @staticmethod
    def _parse_bookmark(b: dict[str, Any], camera_id: int, tz: ZoneInfo) -> Bookmark:
        start = _local_ts(b["startTime"], tz)
        return Bookmark(
            id=int(b["bookmarkId"]),
            camera_id=int(camera_id),
            name=str(b.get("name") or ""),
            comment=str(b.get("comment") or ""),
            start=start,
            end=_local_ts(b["endTime"], tz) if b.get("endTime") else start,
        )

    async def download(
        self, recording_id: int, mount_id: int, offset_ms: int, duration_ms: int, timelapse: bool = False
    ) -> bytes:
        """Cut [offset, offset+duration) out of one recording as MP4 bytes.

        ``timelapse``: the id is a time-lapse file's (offsets in its video time).
        """
        for attempt in (1, 2):
            if self._sid is None:
                await self.login(stale_sid=None)
            sid = self._sid
            params = {
                "api": "SYNO.SurveillanceStation.Recording", "method": "Download", "version": 6,
                "id": recording_id, "mountId": mount_id,
                "offsetTimeMs": max(0, int(offset_ms)), "playTimeMs": max(1, int(duration_ms)),
                "_sid": sid,
            }
            if timelapse:
                params["recEvtType"] = REC_EVT_TIMELAPSE
            body, ctype = await self._request("entry.cgi", params, False, 60)
            if "json" not in ctype and not body.startswith(b"{"):
                return body
            try:
                err = json.loads(body)
            except (ValueError, RecursionError):
                err = {}
            if not isinstance(err, dict):
                err = {}
            code = _error_code(err)
            if code in SESSION_ERRORS and attempt == 1:
                await self.login(stale_sid=sid)
                continue
            raise SSError("SYNO.SurveillanceStation.Recording", "Download", code, err.get("error"))
        raise AssertionError("unreachable")  # pragma: no cover

    async def download_to(
        self,
        fd: int,
        recording_id: int,
        mount_id: int,
        offset_ms: int,
        duration_ms: int,
        timelapse: bool = False,
        timeout: float = 120,
    ) -> int:
        """Like download(), but streamed into the file ``fd`` (bytes written).

        For cuts too big to hold twice in memory: a second of daytime
        time-lapse video is ~30 MB.
        """
        api, method = "SYNO.SurveillanceStation.Recording", "Download"
        for attempt in (1, 2):
            if self._sid is None:
                await self.login(stale_sid=None)
            sid = self._sid
            params: dict[str, Any] = {
                "api": api, "method": method, "version": 6,
                "id": recording_id, "mountId": mount_id,
                "offsetTimeMs": max(0, int(offset_ms)), "playTimeMs": max(1, int(duration_ms)),
                "_sid": sid,
            }
            if timelapse:
                params["recEvtType"] = REC_EVT_TIMELAPSE
            try:
                async with self._session.get(
                    f"{self._base}/entry.cgi", params=params, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status >= 400:
                        raise SSError(api, method, None, f"HTTP {resp.status}")
                    first = await resp.content.read(DOWNLOAD_CHUNK)
                    if "json" in resp.headers.get("Content-Type", "") or first.startswith(b"{"):
                        body = first
                        while len(body) < DOWNLOAD_ERROR_MAX and (more := await resp.content.read(DOWNLOAD_CHUNK)):
                            body += more
                    else:
                        try:
                            written = _write_all(fd, first)
                            async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK):
                                written += _write_all(fd, chunk)
                        except OSError as err:  # out of memory for the file
                            raise SSError(api, method, None, f"writing the cut: {type(err).__name__}") from None
                        return written
            except (aiohttp.ClientError, TimeoutError) as err:
                raise SSConnectionError(api, method, None, type(err).__name__) from None
            try:
                err = json.loads(body)
            except (ValueError, RecursionError):
                err = {}
            if not isinstance(err, dict):
                err = {}
            code = _error_code(err)
            if code in SESSION_ERRORS and attempt == 1:
                await self.login(stale_sid=sid)
                continue
            raise SSError(api, method, code, err.get("error"))
        raise AssertionError("unreachable")  # pragma: no cover


def _write_all(fd: int, data: bytes) -> int:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
    return len(data)


def _quoted(text: str) -> str:
    """A string parameter as SS wants it: JSON-quoted (it strips the quotes)."""
    return json.dumps(text, ensure_ascii=False)


def _local_ts(value: str, tz: ZoneInfo) -> int:
    """NAS-local ISO time (no offset) to epoch seconds.

    In the hour a DST change repeats, the earlier of the two readings is
    taken: SS gives nothing else to tell them apart.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())
