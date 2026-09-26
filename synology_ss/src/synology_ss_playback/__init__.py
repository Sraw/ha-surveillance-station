"""Play back Synology Surveillance Station recordings as HLS."""

from .client import (
    OTP_ERRORS,
    Bookmark,
    Camera,
    RecordingInfo,
    SSAuthError,
    SSConnectionError,
    SSError,
    SSInfo,
    SurveillanceStationClient,
)
from .segment import fetch_segment, fetch_snapshot, recordings_from, remove_stale_temp_files
from .vod import (
    Recording,
    Run,
    Segment,
    live_edge,
    plan_segments,
    render_playlist,
    runs_from_segments,
)

__all__ = [
    "OTP_ERRORS",
    "Bookmark",
    "Camera",
    "Recording",
    "RecordingInfo",
    "Run",
    "SSAuthError",
    "SSConnectionError",
    "SSError",
    "SSInfo",
    "Segment",
    "SurveillanceStationClient",
    "fetch_segment",
    "fetch_snapshot",
    "live_edge",
    "plan_segments",
    "recordings_from",
    "remove_stale_temp_files",
    "render_playlist",
    "runs_from_segments",
]
