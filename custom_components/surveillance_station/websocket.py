"""WebSocket commands used by the timeline card."""

from __future__ import annotations

import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .api import SSConnectionError, SSError, SurveillanceStationClient
from .const import DOMAIN, VOD_MAX_WINDOW_SECONDS, VOD_SESSION_TTL_SECONDS, VOD_URL
from .views import VodManager, VodSession, to_recordings
from .vod import live_edge, plan_segments, runs_from_segments

# A window whose end is at least this close to now becomes a live session.
LIVE_THRESHOLD_SECONDS = 60

ERR_SS = "surveillance_station_error"


@callback
def async_register(hass: HomeAssistant) -> None:
    for handler in (ws_cameras, ws_recordings, ws_bookmarks, ws_vod):
        websocket_api.async_register_command(hass, handler)


def _manager(hass: HomeAssistant) -> VodManager:
    return hass.data[DOMAIN]


def _client(hass: HomeAssistant, entry_id: str | None) -> tuple[str, SurveillanceStationClient]:
    clients = _manager(hass).clients
    if not clients:
        raise KeyError("no Surveillance Station configured")
    if entry_id is None:
        entry_id = next(iter(clients))
    return entry_id, clients[entry_id]


def _range(msg: dict[str, Any]) -> tuple[int, int]:
    start, end = int(msg["start"]), int(msg["end"])
    if end <= start:
        raise ValueError("end must be after start")
    return start, end


async def _run(connection: websocket_api.ActiveConnection, msg: dict[str, Any], coro) -> None:
    try:
        connection.send_result(msg["id"], await coro)
    except (KeyError, ValueError) as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
    except SSConnectionError:
        connection.send_error(msg["id"], ERR_SS, "Surveillance Station is unreachable")
    except SSError as err:
        # SSError text is built from API names and SS error codes only.
        connection.send_error(msg["id"], ERR_SS, str(err))


@websocket_api.websocket_command(
    {vol.Required("type"): "surveillance_station/cameras", vol.Optional("entry_id"): str}
)
@websocket_api.async_response
async def ws_cameras(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    async def go():
        entry_id, client = _client(hass, msg.get("entry_id"))
        return {
            "entry_id": entry_id,
            "cameras": [{"id": c.id, "name": c.name, "enabled": c.enabled} for c in await client.cameras()],
        }

    await _run(connection, msg, go())


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
        return {
            "now": time.time(),
            "recordings": [
                {"id": r.id, "start": r.start, "end": r.end, "live": r.live} for r in recs
            ],
        }

    await _run(connection, msg, go())


@websocket_api.websocket_command({vol.Required("type"): "surveillance_station/bookmarks", **_RANGE_SCHEMA})
@websocket_api.async_response
async def ws_bookmarks(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    async def go():
        _, client = _client(hass, msg.get("entry_id"))
        start, end = _range(msg)
        return {
            "bookmarks": [
                {"id": b.id, "name": b.name, "comment": b.comment, "start": b.start, "end": b.end}
                for b in await client.bookmarks(msg["camera_id"], start, end)
            ]
        }

    await _run(connection, msg, go())


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
        segments = plan_segments(to_recordings(infos), start, end, now)
        if not segments:
            return {"url": None, "runs": [], "start": start, "end": end, "live": False}
        session = VodSession(
            entry_id, msg["camera_id"], start, segments, now + VOD_SESSION_TTL_SECONDS,
            live=live, max_end=max_end, planned_end=end,
        )
        token = _manager(hass).create_session(session)
        return {
            "url": f"{VOD_URL}/{token}/index.m3u8",
            "live": live,
            "start": segments[0].wall_start,
            "end": segments[-1].wall_start + segments[-1].duration,
            "runs": [
                {"wall_start": r.wall_start, "media_start": r.media_start, "duration": r.duration}
                for r in runs_from_segments(segments)
            ],
        }

    await _run(connection, msg, go())
