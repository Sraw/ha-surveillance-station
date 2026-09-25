"""Minimal async client for the Synology Surveillance Station Web API.

Only what playback needs: login, cameras, recordings (with start/stop times),
bookmarks (listed, and created/edited/deleted for detections from elsewhere),
and cutting a time range out of a recording.

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
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Error codes that mean "log in again and retry" (common DSM Web API codes).
SESSION_ERRORS = {105, 106, 107, 119}
AUTH_FAILED_ERRORS = {400, 401, 402, 403, 404, 406, 407, 408, 409, 410}
VIDEO_CODEC_H265 = 6
# SS splits continuous recordings into files of at most this length (the
# per-camera setting tops out well below it).
RECORDING_LOOKBACK_SECONDS = 4 * 3600
# A freshly opened live socket that sends nothing at all (SS wedged) within
# this long is given up on rather than held forever.
LIVE_CONNECT_TIMEOUT_SECONDS = 15


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
        self._base = f"{'https' if use_https else 'http'}://{host}:{port}/webapi"
        self._username = username
        self._password = password
        self._sid: str | None = None
        self._login_lock = asyncio.Lock()
        self._auth_failed: int | None = None
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
            if self._auth_failed is not None:
                # Credentials were refused once; retrying on every call would
                # trip DSM's auto-block for this host within minutes. A
                # successful reauth reloads the entry with a new client.
                raise SSAuthError("SYNO.API.Auth", "login", self._auth_failed)
            if self._sid is not None and self._sid != stale_sid:
                return
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
                code = data.get("error", {}).get("code")
                if code in AUTH_FAILED_ERRORS:
                    self._auth_failed = code
                    self._sid = None
                    if self.on_auth_failed is not None:
                        self.on_auth_failed()
                    raise SSAuthError("SYNO.API.Auth", "login", code)
                raise SSError("SYNO.API.Auth", "login", code)
            self._sid = data["data"]["sid"]

    async def logout(self) -> None:
        if self._sid is None:
            return
        try:
            await self._raw_json(
                "auth.cgi",
                {"api": "SYNO.API.Auth", "method": "logout", "version": 6,
                 "session": "SurveillanceStation", "_sid": self._sid},
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
            now = time.monotonic()
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

    async def _raw_json(self, path: str, params: dict[str, Any], post: bool = False) -> dict[str, Any]:
        body, _ = await self._request(path, params, post, 30)
        try:
            return json.loads(body)
        except ValueError:
            raise SSError(params.get("api", path), params.get("method", ""), None, "non-JSON reply") from None

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
                return data.get("data") or {}
            code = data.get("error", {}).get("code")
            if code in SESSION_ERRORS and attempt == 1:
                _LOGGER.debug("Session error %s on %s.%s, logging in again", code, api, method)
                await self.login(stale_sid=sid)
                continue
            raise SSError(api, method, code, data.get("error"))
        raise AssertionError("unreachable")  # pragma: no cover

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
        except ZoneInfoNotFoundError:
            _LOGGER.warning("Unknown NAS time zone %r, assuming UTC", info.timezone)
            self._tz = ZoneInfo("UTC")
        return info

    async def _timezone(self) -> ZoneInfo:
        if self._tz is None:
            await self.info()
        return self._tz or ZoneInfo("UTC")

    async def cameras(self) -> list[Camera]:
        data = await self._call("SYNO.SurveillanceStation.Camera", "List", 9)
        return [
            Camera(
                id=int(c["id"]),
                name=c.get("newName") or c.get("name") or str(c["id"]),
                enabled=bool(c.get("enabled", True)),
            )
            for c in data.get("cameras", [])
        ]

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
            events = data.get("events", [])
            for e in events:
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
            offset += len(events)
            if not events or offset >= int(data.get("total", 0)):
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
        for b in data.get("bookmarks") or []:
            start = _local_ts(b["startTime"], tz)
            out.append(
                Bookmark(
                    id=int(b["bookmarkId"]),
                    camera_id=int(b["camId"]),
                    name=b.get("name") or "",
                    comment=b.get("comment") or "",
                    start=start,
                    end=_local_ts(b["endTime"], tz) if b.get("endTime") else start,
                )
            )
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
        b = (data.get("bookmark") or [None])[0]
        if not b or "bookmarkId" not in b:
            raise SSError("SYNO.SurveillanceStation.ThirdParty.Bookmark", method, None, data)
        start = _local_ts(b["startTime"], tz)
        return Bookmark(
            id=int(b["bookmarkId"]),
            camera_id=int(camera_id),
            name=b.get("name") or "",
            comment=b.get("comment") or "",
            start=start,
            end=_local_ts(b["endTime"], tz) if b.get("endTime") else start,
        )

    async def download(self, recording_id: int, mount_id: int, offset_ms: int, duration_ms: int) -> bytes:
        """Cut [offset, offset+duration) out of one recording as MP4 bytes."""
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
            body, ctype = await self._request("entry.cgi", params, False, 60)
            if "json" not in ctype and not body.startswith(b"{"):
                return body
            try:
                err = json.loads(body)
            except ValueError:
                err = {}
            code = err.get("error", {}).get("code")
            if code in SESSION_ERRORS and attempt == 1:
                await self.login(stale_sid=sid)
                continue
            raise SSError("SYNO.SurveillanceStation.Recording", "Download", code, err.get("error"))
        raise AssertionError("unreachable")  # pragma: no cover


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
