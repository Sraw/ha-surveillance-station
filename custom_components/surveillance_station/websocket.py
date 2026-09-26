"""WebSocket commands used by the timeline card."""

from __future__ import annotations

import time
from typing import Any

from synology_ss_playback import (
    Bookmark,
    SSConnectionError,
    SSError,
    SurveillanceStationClient,
    live_edge,
    plan_segments,
    recordings_from,
    runs_from_segments,
)
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import (
    BOOKMARK_PAGE_MAX,
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


@callback
def async_register(hass: HomeAssistant) -> None:
    for handler in (ws_cameras, ws_recordings, ws_bookmarks, ws_bookmark_page, ws_search, ws_live, ws_vod, ws_vod_runs):
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
        vol.Optional("kinds"): [str],
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
        return {
            "total": len(matching),
            "more": len(rest) > len(page),
            "kinds": _kind_counts(shown),
            "bookmarks": [
                _bookmark(b, _manager(hass).sign_thumbnail(entry_id, b.camera_id, _manager(hass).frame(entry_id, b)))
                for b in page
            ],
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
            camera_ids=msg.get("camera_ids"), limit=msg["limit"],
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
        return _describe(session)

    await _run(hass, connection, msg, go())
