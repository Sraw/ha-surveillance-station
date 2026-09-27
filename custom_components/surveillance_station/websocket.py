"""WebSocket commands used by the timeline card."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import time
from typing import Any
from zoneinfo import ZoneInfo

from synology_ss_playback import (
    CODECS,
    Bookmark,
    SSConnectionError,
    SSError,
    SurveillanceStationClient,
    TimelapseRecording,
    TranscodeSpec,
    covered,
    live_edge,
    output_size,
    plan_day,
    plan_segments,
    recordings_from,
    runs_from_segments,
)
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import (
    BOOKMARK_PAGE_MAX,
    CONF_TRANSCODER,
    DEFAULT_TRANSCODER,
    FRIGATE_SEARCH_MAX,
    KIND_CHIPS_MAX,
    DOMAIN,
    LIVE_END_STALE_SECONDS,
    LIVE_URL,
    MAX_QUERY_WINDOW_SECONDS,
    VOD_MAX_WINDOW_SECONDS,
    VOD_SESSION_TTL_SECONDS,
    VOD_URL,
)
from .frigate import DATA_FRIGATE, name_kinds
from .frigate_api import FrigateAPIError
from .search import search
from .views import DATA_MANAGER, VodManager, VodSession

# A window whose end is at least this close to now becomes a live session.
LIVE_THRESHOLD_SECONDS = 60

ERR_SS = "surveillance_station_error"
ERR_FRIGATE = "frigate_error"
ERR_GPU_BUSY = "gpu_busy"


@callback
def async_register(hass: HomeAssistant) -> None:
    for handler in (
        ws_cameras, ws_recordings, ws_bookmarks, ws_bookmark_page, ws_search, ws_live, ws_vod, ws_vod_runs,
        ws_timelapse_days, ws_timelapse,
    ):
        websocket_api.async_register_command(hass, handler)


def _manager(hass: HomeAssistant) -> VodManager:
    return hass.data[DATA_MANAGER]


def _client(hass: HomeAssistant, entry_id: str | None) -> tuple[str, SurveillanceStationClient]:
    """The asked-for entry's client, or the first loaded one's."""
    if entry_id is None:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise KeyError("no Surveillance Station is set up")
        entry_id = entries[0].entry_id
    if (client := _manager(hass).client(entry_id)) is None:
        raise KeyError(f"Surveillance Station entry {entry_id} is not loaded")
    return entry_id, client


class TranscodeUnavailable(Exception):
    """Nothing may transcode now, per the entry's transcoding option."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _transcode_on_gpu(hass: HomeAssistant, entry_id: str, wait: bool = True) -> bool:
    """Whether the entry's video is transcoded on the GPU (True) or the CPU (False).

    Per its transcoding option: "cpu" never asks the GPU; "gpu" and "auto"
    need a GPU check that answered - one that timed out (the GPU busy) is
    no reason to load the CPU instead - and "gpu" a GPU. Without ``wait``,
    a check still to come is started and counts as not answered.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    mode = entry.options.get(CONF_TRANSCODER, DEFAULT_TRANSCODER) if entry else DEFAULT_TRANSCODER
    if mode == "cpu":
        return False
    manager = _manager(hass)
    found = await manager.hardware() if wait else manager.check_hardware()
    if found is None:
        raise TranscodeUnavailable(ERR_GPU_BUSY, "The Intel GPU didn't answer in time; try again")
    if not found and mode == "gpu":
        raise TranscodeUnavailable(
            websocket_api.ERR_NOT_SUPPORTED, "Transcoding is set to the GPU only, and Home Assistant has no usable Intel GPU"
        )
    return found


def _range(msg: dict[str, Any]) -> tuple[int, int]:
    start, end = int(msg["start"]), int(msg["end"])
    if end <= start:
        raise ValueError("end must be after start")
    if end - start > MAX_QUERY_WINDOW_SECONDS:
        raise ValueError(f"window is longer than {MAX_QUERY_WINDOW_SECONDS // 86400} days")
    return start, end


async def _run(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any], coro
) -> None:
    manager = _manager(hass)
    entry_id = msg.get("entry_id")
    try:
        result = await coro
    except (KeyError, ValueError) as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    except SSConnectionError as err:
        if entry_id or (entry_id := _first_entry_id(hass)):
            manager.track(entry_id, err)
        connection.send_error(msg["id"], ERR_SS, "Surveillance Station is unreachable")
        return
    except SSError as err:
        # SSError text is built from API names and SS error codes only.
        connection.send_error(msg["id"], ERR_SS, str(err))
        return
    except FrigateAPIError as err:
        # Ours too: a path, a status, Frigate's own message.
        connection.send_error(msg["id"], ERR_FRIGATE, str(err))
        return
    except TranscodeUnavailable as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    if entry_id or (entry_id := _first_entry_id(hass)):
        manager.track(entry_id, None)
    connection.send_result(msg["id"], result)


def _first_entry_id(hass: HomeAssistant) -> str | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].entry_id if entries else None


@websocket_api.websocket_command(
    {vol.Required("type"): "surveillance_station/cameras", vol.Optional("entry_id"): str}
)
@websocket_api.async_response
async def ws_cameras(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    async def go():
        entry_id, client = _client(hass, msg.get("entry_id"))
        bridge = hass.data.get(DATA_FRIGATE, {}).get(entry_id)
        return {
            "entry_id": entry_id,
            "cameras": [{"id": c.id, "name": c.name, "enabled": c.enabled} for c in await client.cameras()],
            # Frigate's smart search can be asked (its URL is in the options).
            "search": bridge is not None and bridge.api is not None,
        }

    await _run(hass, connection, msg, go())


def _describe(session: VodSession) -> dict[str, Any]:
    segments = session.segments
    return {
        "live": session.live,
        "start": segments[0].wall_start,
        "end": segments[-1].wall_start + segments[-1].duration,
        "runs": [
            {"wall_start": r.wall_start, "media_start": r.media_start, "duration": r.duration}
            for r in runs_from_segments(segments)
        ],
    }


_RANGE_SCHEMA = {
    vol.Optional("entry_id"): str,
    vol.Required("camera_id"): vol.Coerce(int),
    vol.Required("start"): vol.Coerce(float),
    vol.Required("end"): vol.Coerce(float),
}


@websocket_api.websocket_command({vol.Required("type"): "surveillance_station/recordings", **_RANGE_SCHEMA})
@websocket_api.async_response
async def ws_recordings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    async def go():
        _, client = _client(hass, msg.get("entry_id"))
        start, end = _range(msg)
        recs = await client.recordings(msg["camera_id"], start, end)
        now = time.time()
        return {
            "now": now,
            "recordings": [
                {"id": r.id, "start": r.start, "end": r.end, "live": r.live and now - r.end <= LIVE_END_STALE_SECONDS}
                for r in recs
            ],
        }

    await _run(hass, connection, msg, go())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "surveillance_station/bookmarks",
        vol.Optional("entry_id"): str,
        # Omitted: every camera.
        vol.Optional("camera_ids"): [vol.Coerce(int)],
        vol.Required("start"): vol.Coerce(float),
        vol.Required("end"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def ws_bookmarks(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Bookmarks overlapping a time window, oldest first (the timeline)."""

    async def go():
        entry_id, client = _client(hass, msg.get("entry_id"))
        start, end = _range(msg)
        cams = msg.get("camera_ids")
        found = [
            b
            for b in await _manager(hass).bookmarks(entry_id, client)
            if b.end >= start and b.start <= end and (cams is None or b.camera_id in cams)
        ]
        return {"bookmarks": [_bookmark(b) for b in reversed(found)]}

    await _run(hass, connection, msg, go())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "surveillance_station/bookmark_page",
        vol.Optional("entry_id"): str,
        # Omitted: every camera.
        vol.Optional("camera_ids"): [vol.Coerce(int)],
        # Cursor: the last bookmark of the previous page. Omitted: the newest.
        vol.Inclusive("before", "cursor"): vol.Coerce(int),
        vol.Inclusive("before_id", "cursor"): vol.Coerce(int),
        vol.Optional("limit", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=BOOKMARK_PAGE_MAX)),
        # Only bookmarks of these kinds ("Person", "Car": the parts of a name
        # like "Person, Car", ignoring case). Omitted or empty: all.
        vol.Optional("kinds"): vol.All([vol.All(str, vol.Length(max=64))], vol.Length(max=16)),
    }
)
@websocket_api.async_response
async def ws_bookmark_page(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Every bookmark SS has, newest first, a page at a time (the event list).

    Paged by a cursor rather than an offset, so bookmarks created while the
    list is open don't shift the pages (they show up on the next first page).
    """

    async def go():
        entry_id, client = _client(hass, msg.get("entry_id"))
        cams = msg.get("camera_ids")
        shown = [b for b in await _manager(hass).bookmarks(entry_id, client) if cams is None or b.camera_id in cams]
        wanted = {k.strip().casefold() for k in msg.get("kinds") or [] if k.strip()}
        matching = [b for b in shown if not wanted or wanted & {k.casefold() for k in name_kinds(b.name)}]
        if "before" in msg:
            cursor = (msg["before"], msg["before_id"])
            rest = [b for b in matching if (b.start, b.id) < cursor]
        else:
            rest = matching
        page = rest[: msg["limit"]]
        # Frigate's bookmarks: Frigate's snapshot, as the notification's image.
        bridge = hass.data.get(DATA_FRIGATE, {}).get(entry_id)
        manager = _manager(hass)

        def thumbnail(b: Bookmark) -> str:
            if bridge is not None:
                return bridge.bookmark_thumbnail(b)
            return manager.sign_thumbnail(entry_id, b.camera_id, manager.frame(entry_id, b))

        return {
            "total": len(matching),
            "more": len(rest) > len(page),
            "kinds": _kind_counts(shown),
            "bookmarks": [_bookmark(b, thumbnail(b)) for b in page],
        }

    await _run(hass, connection, msg, go())


def _kind_counts(bookmarks: list[Bookmark]) -> list[list[Any]]:
    """The kinds to filter the event list by: those of more than one bookmark
    (a hand-made bookmark's own name is no kind), most common first."""
    counts: dict[str, int] = {}
    spellings: dict[str, dict[str, int]] = {}  # "car": {"Car": 3, "car": 1}
    for b in bookmarks:
        for k in name_kinds(b.name):
            key = k.casefold()
            counts[key] = counts.get(key, 0) + 1
            spellings.setdefault(key, {})[k] = spellings.setdefault(key, {}).get(k, 0) + 1
    common = sorted((kc for kc in counts.items() if kc[1] > 1), key=lambda kc: (-kc[1], kc[0]))
    # Each kind as it is mostly written.
    return [[max(spellings[k].items(), key=lambda s: s[1])[0], n] for k, n in common[:KIND_CHIPS_MAX]]


@websocket_api.websocket_command(
    {
        vol.Required("type"): "surveillance_station/search",
        vol.Optional("entry_id"): str,
        vol.Exclusive("query", "by"): vol.All(str, vol.Length(min=1, max=200)),
        vol.Exclusive("bookmark_id", "by"): vol.Coerce(int),
        vol.Optional("camera_ids"): [vol.Coerce(int)],
        # Only bookmarks of these kinds (as bookmark_page's kinds).
        vol.Optional("kinds"): vol.All([vol.All(str, vol.Length(max=64))], vol.Length(max=16)),
        vol.Optional("limit", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=FRIGATE_SEARCH_MAX)),
    }
)
@websocket_api.async_response
async def ws_search(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Frigate's smart search (words, or "similar to" a bookmark), results placed on SS."""

    async def go():
        entry_id, _ = _client(hass, msg.get("entry_id"))
        bridge = hass.data.get(DATA_FRIGATE, {}).get(entry_id)
        if bridge is None:
            raise ValueError("smart search needs Frigate detections turned on in the integration's options")
        results = await search(
            _manager(hass), entry_id, bridge,
            query=msg.get("query"), bookmark_id=msg.get("bookmark_id"),
            camera_ids=msg.get("camera_ids"), kinds=msg.get("kinds"), limit=msg["limit"],
        )
        return {"results": results}

    await _run(hass, connection, msg, go())


def _bookmark(b: Bookmark, thumbnail: str | None = None) -> dict[str, Any]:
    """A bookmark for the card; the timeline's have no thumbnail to sign."""
    out = {
        "id": b.id,
        "camera_id": b.camera_id,
        "name": b.name,
        "comment": b.comment,
        "start": b.start,
        "end": b.end,
    }
    if thumbnail is not None:
        out["thumbnail"] = thumbnail
    return out


@websocket_api.websocket_command(
    {
        vol.Required("type"): "surveillance_station/live",
        vol.Optional("entry_id"): str,
        vol.Required("camera_id"): vol.Coerce(int),
        # Epoch seconds: play the recordings from then on. Omitted: real time.
        vol.Optional("time"): vol.All(vol.Coerce(float), vol.Range(min=0, max=2**32)),
    }
)
@websocket_api.async_response
async def ws_live(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A single-use URL for a camera's stream (a WebSocket, see LiveStreamView)."""

    async def go():
        entry_id, _ = _client(hass, msg.get("entry_id"))
        token = _manager(hass).create_live_token(entry_id, msg["camera_id"], msg.get("time"))
        return {"url": f"{LIVE_URL}/{token}"}

    await _run(hass, connection, msg, go())


@websocket_api.websocket_command({vol.Required("type"): "surveillance_station/vod", **_RANGE_SCHEMA})
@websocket_api.async_response
async def ws_vod(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Create a playback session for [start, end) and return its playlist URL.

    ``runs`` maps playlist time to wall-clock time (a new run starts after
    every gap in the recordings), so the card can show and seek by clock time.
    A window reaching the present is ``live``: its playlist keeps growing,
    and media time past the last run continues that run linearly.
    """

    async def go():
        entry_id, client = _client(hass, msg.get("entry_id"))
        start, end = _range(msg)
        # Whole seconds: SS floors cut offsets to a keyframe (1 s GOP), so a
        # fractional start would begin the first segment early.
        start = float(int(start))
        now = time.time()
        max_end = start + VOD_MAX_WINDOW_SECONDS
        live = end >= now - LIVE_THRESHOLD_SECONDS and now < max_end
        end = min(live_edge(now) if live else min(end, now), max_end)
        if end <= start:
            raise ValueError("window is in the future")
        infos = await client.recordings(msg["camera_id"], int(start), int(end) + 1)
        segments = plan_segments(recordings_from(infos), start, end, now)
        if not segments:
            return {"url": None, "runs": [], "start": start, "end": end, "live": False}
        session = VodSession(
            entry_id, msg["camera_id"], start, segments, now + VOD_SESSION_TTL_SECONDS,
            live=live, max_end=max_end, planned_end=end,
        )
        token = _manager(hass).create_session(session)
        return {"url": f"{VOD_URL}/{token}/index.m3u8", **_describe(session)}

    await _run(hass, connection, msg, go())


@websocket_api.websocket_command(
    {vol.Required("type"): "surveillance_station/vod_runs", vol.Required("token"): str}
)
@websocket_api.async_response
async def ws_vod_runs(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Current mapping of a session; a live one grows (and may gain gaps)."""

    async def go():
        session = _manager(hass).get_session(msg["token"])
        if session is None:
            raise KeyError("playback session expired")
        if session.transcode:
            raise KeyError("not a recordings session (time-lapse runs come with the session)")
        return _describe(session)

    await _run(hass, connection, msg, go())


def _day_bounds(day: date, tz: ZoneInfo) -> tuple[float, float]:
    """[midnight, next midnight) of a NAS-local day (23 or 25 h across DST)."""
    return _local(day, tz, 0), _local(day + timedelta(days=1), tz, 0)


def _local(day: date, tz: ZoneInfo, hour: int) -> float:
    return datetime(day.year, day.month, day.day, hour, tzinfo=tz).timestamp()


def _camera_files(files: list[TimelapseRecording], camera_id: int) -> list[TimelapseRecording]:
    """A camera's files, of one task: the one it recorded most recently with.

    Two tasks of one camera (different rates, overlapping days) can't be
    stitched into one day; SS's UI keeps them apart too.
    """
    mine = [f for f in files if f.camera_id == camera_id]
    if not mine:
        return []
    task = max(mine, key=lambda f: f.start).task_id
    return [f for f in mine if f.task_id == task]


def _days(files: list[TimelapseRecording], tz: ZoneInfo) -> list[dict[str, Any]]:
    """The NAS-local days one camera's files cover, newest first, with the stretches covered."""
    stretches = sorted(covered(f) for f in files)
    days: dict[date, list[list[float]]] = {}
    for lo, hi in stretches:
        if hi - lo < 60:
            continue
        day = datetime.fromtimestamp(lo, tz).date()
        while True:
            start, end = _day_bounds(day, tz)
            if start >= hi:
                break
            a, b = max(lo, start), min(hi, end)
            if b > a:
                spans = days.setdefault(day, [])
                if spans and a <= spans[-1][1] + 60:
                    spans[-1][1] = max(spans[-1][1], b)
                else:
                    spans.append([a, b])
            day += timedelta(days=1)
    out = []
    for day in sorted(days, reverse=True):
        start, end = _day_bounds(day, tz)
        if not plan_day(files, start, end)[1]:
            continue  # minutes past midnight only: less than the whole video second a day starts on
        out.append({
            "date": day.isoformat(), "start": start, "end": end, "covered": days[day],
            # Where 06:00, 12:00 and 18:00 fall (not a quarter of the day apart across DST).
            "hours": {h: _local(day, tz, h) for h in (6, 12, 18)},
        })
    return out


@websocket_api.websocket_command(
    {vol.Required("type"): "surveillance_station/timelapse_days", vol.Optional("entry_id"): str}
)
@websocket_api.async_response
async def ws_timelapse_days(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The cameras with a time-lapse task, and the days (NAS-local) each can show.

    ``hardware``: whether it would be transcoded on the GPU, or None if
    that isn't known yet or it can't be now. Not waited for: the GPU check
    is only started here, so that opening a day finds it done or under way.
    """

    async def go():
        entry_id, client = _client(hass, msg.get("entry_id"))
        manager = _manager(hass)
        files = await manager.timelapse_files(entry_id, client)
        tz = await client.timezone()
        names = {c.id: c.name for c in await client.cameras()}
        by_camera = {cid: _camera_files(files, cid) for cid in {f.camera_id for f in files}}
        try:
            hardware = await _transcode_on_gpu(hass, entry_id, wait=False)
        except TranscodeUnavailable:
            hardware = None
        return {
            "entry_id": entry_id,
            "timezone": str(tz),
            "hardware": hardware,
            "cameras": [
                {"id": cid, "name": names.get(cid, str(cid)), "days": _days(by_camera[cid], tz)}
                for cid in sorted(by_camera, key=lambda c: names.get(c, str(c)).lower())
            ],
        }

    await _run(hass, connection, msg, go())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "surveillance_station/timelapse",
        vol.Optional("entry_id"): str,
        vol.Required("camera_id"): vol.Coerce(int),
        vol.Required("date"): vol.All(str, vol.Length(max=10), vol.Match(r"^\d{4}-\d{2}-\d{2}$")),
        vol.Optional("codec", default="hevc"): vol.In(CODECS),
    }
)
@websocket_api.async_response
async def ws_timelapse(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A playback session for one camera's time-lapse of one NAS-local day.

    Its playlist and segments are the VOD views' (the same token scheme);
    every segment is transcoded to ``codec`` (H.264 only without a GPU).
    ``runs`` maps playlist time to wall time: each run is a stretch of one
    file, played ``rate`` times faster than real time.
    """

    async def go():
        entry_id, client = _client(hass, msg.get("entry_id"))
        manager = _manager(hass)
        day = date.fromisoformat(msg["date"])
        tz = await client.timezone()
        try:
            start, end = _day_bounds(day, tz)
        except OverflowError:
            raise ValueError("date out of range") from None
        files = _camera_files(await manager.timelapse_files(entry_id, client), msg["camera_id"])
        segments, runs = plan_day(files, start, end)
        result: dict[str, Any] = {"date": day.isoformat(), "start": start, "end": end}
        if not segments:  # nothing to transcode: whatever the GPU
            return {**result, "url": None, "duration": 0, "runs": []}
        hardware = await _transcode_on_gpu(hass, entry_id)
        codec = msg["codec"] if hardware else "h264"
        result |= {"codec": codec, "hardware": hardware}
        by_id = {f.id: f for f in files}
        transcode = {}
        for rid in {seg.recording_id for seg in segments}:
            f = by_id[rid]
            width, height = output_size(f.width, f.height)
            transcode[rid] = TranscodeSpec(codec, hardware, f.hevc, width, height)
        session = VodSession(
            entry_id, msg["camera_id"], start, segments, time.time() + VOD_SESSION_TTL_SECONDS, transcode=transcode
        )
        token = manager.create_timelapse_session(session)
        last = segments[-1]
        return {
            **result,
            "url": f"{VOD_URL}/{token}/index.m3u8",
            "duration": last.media_start + last.duration,
            "runs": [
                {"media_start": r.media_start, "duration": r.duration, "wall_start": r.wall_start, "rate": r.rate}
                for r in runs
            ],
            # Where the segments are, for a player that fetches them itself:
            # <index i> starts at media time segments[i]. Every file's first
            # segment has an init segment (init/<i>.mp4, the playlist's
            # EXT-X-MAP); files transcoded to the same size give identical ones.
            "segments": [s.media_start for s in segments],
            "maps": [
                {"index": s.index, "size": f"{transcode[s.recording_id].width}x{transcode[s.recording_id].height}"}
                for s in segments
                if s.new_map
            ],
        }

    await _run(hass, connection, msg, go())
