"""Minimal async client for the Synology Surveillance Station Web API.

Only what playback needs: login, cameras, recordings (with start/stop times),
bookmarks, and cutting a time range out of a recording.

API notes (verified against SS 9.x, see the repo README):
* ``SYNO.SurveillanceStation.Event`` ``List`` v5 is the call that returns
  ``startTime``/``stopTime`` per recording; ``Recording.List`` v6 does not.
* Both ``Event.List`` and ``Recording.List`` filter ``fromTime``/``toTime`` on
  a recording's *start* time, not on overlap, so a query must reach back by
  the longest recording length (``RECORDING_LOOKBACK_SECONDS``) and then
  filter by overlap itself.
* Bookmarks come embedded in ``Recording.List`` **v5** results.
* ``Recording.Download`` v6 with ``offsetTimeMs``/``playTimeMs`` returns an
  MP4 (moov at the end, no Range support). On failure it returns JSON.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Error codes that mean "log in again and retry" (common DSM Web API codes).
SESSION_ERRORS = {105, 106, 107, 119}
AUTH_FAILED_ERRORS = {400, 401, 402, 403, 404, 406, 407, 408, 409, 410}
VIDEO_CODEC_H265 = 6
# SS splits continuous recordings into files of at most this length (the
# per-camera setting tops out well below it).
RECORDING_LOOKBACK_SECONDS = 4 * 3600


def _is_hevc(codec: Any) -> bool:
    """SS reports videoCodec as an int (6 = H.265) or, in some calls, a name."""
    if isinstance(codec, int):
        return codec == VIDEO_CODEC_H265
    return str(codec or "").upper() in ("H265", "HEVC")


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
        raise AssertionError("unreachable")

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

    async def bookmarks(self, camera_id: int, start: int, end: int) -> list[Bookmark]:
        """Bookmarks of one camera that overlap [start, end], oldest first."""
        seen: set[int] = set()
        out: list[Bookmark] = []
        offset = 0
        while True:
            data = await self._call(
                "SYNO.SurveillanceStation.Recording", "List", 5,
                cameraIds=str(camera_id), fromTime=int(start) - RECORDING_LOOKBACK_SECONDS,
                toTime=int(end), offset=offset, limit=200,
            )
            events = data.get("events", [])
            for e in events:
                out.extend(self._bookmarks_of(e, camera_id, start, end, seen))
            offset += len(events)
            if not events or offset >= int(data.get("total", 0)):
                break
        out.sort(key=lambda b: b.start)
        return out

    @staticmethod
    def _bookmarks_of(e: dict[str, Any], camera_id: int, start: int, end: int, seen: set[int]):
        for b in e.get("bookmark") or []:
            bid = int(b["id"])
            ts = int(b.get("timestamp") or 0)
            stop = int(b.get("endtime") or ts)
            if bid in seen or stop < start or ts > end:
                continue
            seen.add(bid)
            yield Bookmark(
                id=bid,
                camera_id=int(b.get("cameraId") or camera_id),
                name=b.get("name") or "",
                comment=b.get("comment") or "",
                start=ts,
                end=stop,
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
        raise AssertionError("unreachable")
