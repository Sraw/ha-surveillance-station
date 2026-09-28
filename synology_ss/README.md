# synology-ss-playback

Async Python library behind the `surveillance_station` Home Assistant
integration. It has no Home Assistant dependency.

- `SurveillanceStationClient`: login (with session renewal), cameras,
  recordings with start/stop times (the same lookup shared for 2 s),
  bookmarks, and cutting a time range out of a recording
  (`Recording.Download`). `close()` logs out for good: later calls raise
  instead of logging in again.
- `plan_segments` / `render_playlist`: cover a wall-clock window with 10 s HLS
  segments cut from those recordings, and the playlist text.
- `fetch_segment`: download one segment and stream-copy it into fragmented MP4
  with ffmpeg, split into the init and media parts.
- Every failure is an `SSError`; `SSAuthError` (credentials refused),
  `SSConnectionError` (NAS unreachable) and `FFmpegError` (ffmpeg or its
  scratch file, here) say which kind.

The Surveillance Station API behaviour it relies on is written up in the
repository README.

Tests: `python3 -m unittest discover -s synology_ss/tests` (from the repo root).
