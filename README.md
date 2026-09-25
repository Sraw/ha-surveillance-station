# Surveillance Station Playback for Home Assistant

Play back **Synology Surveillance Station** recordings inside Home Assistant,
with a scrubbable wall-clock timeline and SS bookmarks drawn on it. SS keeps
recording, archiving and doing timelapse. This integration only reads from it.
Video is never transcoded: SS cuts the requested range out of its own
recording, HA stream-copies it into fragmented MP4, and the browser decodes the
original H.265/H.264.

Status: step 1 of a larger plan (integration + test card). Next come an
Advanced Camera Card engine and notification deep links.

## Pieces

| Part | What it does |
|---|---|
| `synology_ss/` | The protocol library **`synology-ss-playback`** (no HA imports, own `pyproject.toml` and tests; ready for PyPI, not published yet): the SS Web API client (session renewal on 105/106/107/119, SS info, cameras, recordings, bookmarks, `Recording.Download` range cuts), the 10 s segment planner and playlist renderer, and `fetch_segment` (download + ffmpeg remux + fMP4 split) |
| `custom_components/surveillance_station/` | The integration, a thin layer over the library: config flow (user / reauth / reconfigure, unique ID = NAS serial), `entry.runtime_data` = the logged-in client, diagnostics |
| `…/views.py` | HLS VOD endpoints `/api/surveillance_station/vod/<token>/…`: playback sessions, the byte-bounded segment cache, the fetch queue |
| `…/websocket.py` | `surveillance_station/cameras`, `/recordings`, `/bookmarks`, `/vod`, `/vod_runs` |
| `…/frontend/ss-timeline-card.js` | `custom:ss-timeline-card`, registered by the integration as a Lovelace resource. Uses hls.js 1.7.3 (Apache-2.0, vendored) |

## Install

1. Create a dedicated DSM user for HA. It needs Surveillance Station access
   (a viewer-level SS privilege profile that can play back and download
   recordings) and nothing else.
2. `HA_CONFIG=/path/to/config [HA_CONTAINER=homeassistant] scripts/deploy.sh`,
   then restart HA. It copies the integration into `custom_components/` and
   installs the library into `<config>/deps` (the user site the HA container
   puts on `sys.path`), since it isn't on PyPI yet. That install survives HA
   image updates; after an update to a newer Python, run it again.
3. Settings → Devices & services → Add → *Surveillance Station Playback*: host,
   port (5000 http / 5001 https), the DSM user.
4. Add the card to a dashboard (`scripts/dashboard.py` creates a test dashboard
   `/ss-playback` from `dashboards/ss-playback.json`).

```yaml
type: custom:ss-timeline-card
cameras: [Drive Way, Front Door]   # optional: cameras shown (default: all)
camera: Drive Way     # optional: the one to start on
span: 3600            # optional: timeline width in seconds
clock: false          # optional: hide the date/time overlay
```

- **Cameras shown**: one camera is the single view, several are a grid
  (2 side by side, 3-4 in 2x2, more in 3 columns). The camera chips add or
  remove one (outlined = shown, filled = master; the last one stays). The
  square button shows just the master, and switches back to the previous set.
  Double-tapping a grid cell does the same for that camera. The chips' choice
  is remembered per browser (`localStorage`) and wins over `cameras`.
- **Master**: tap a cell. It has the sound and the clock, and the others
  follow it every 500 ms: a drift over 2 s (more at 4x/8x) is a seek, a smaller
  one over 0.1 s nudges `playbackRate` by up to ±20 %. A camera with no
  recording at that time pauses under a "No recording" veil. Seek / skip /
  Latest apply to all cameras.
- **Timeline and Events cover the cameras shown**: recording bars are the time
  where any of them recorded, and the bookmarks on the timeline and in the
  list are the same set. To see another camera's events, show it.
- **Clock**: the overlay button toggles it; remembered per browser.
- **Fullscreen**: the stage (video + a slim auto-hiding control bar) goes
  fullscreen and asks for landscape (`screen.orientation.lock`; honoured on
  Android / the companion app, ignored where the browser doesn't allow it).
  On an iPhone, which has no element fullscreen, the master's own video player
  goes fullscreen instead.
- **Zoom**: pinch or double-tap (single view) zooms up to 8x, drag pans,
  double-tap resets; the mouse wheel zooms in fullscreen. At 1x vertical
  swipes still scroll the page (`touch-action: pan-y`).

Below the timeline, the **Events** list shows the SS bookmarks of the cameras
shown (last 24 h / 3 d / 7 d, grouped by day). Tapping one makes that camera
the master and plays from 3 s before it. The event being
watched is highlighted. SS's own motion detections are not exposed by any
documented API (in continuous mode every `Event` is a recording file), so
bookmarks are the event source; the detection pipeline writes one per
detection through an SS webhook.

URL parameters override on load: `?ss_camera=<name|id>&ss_time=<epoch seconds>`.
A notification can link straight to a moment this way.

A window that reaches the present is **live**: its playlist is an HLS EVENT
playlist that grows as SS records (segments are only published once they end
on the 10 s grid behind real time, so they never change afterwards). "Latest"
therefore plays continuously about 20 s behind real time. Older windows are
closed VOD playlists. When one ends, the card looks up the next recording and
carries on.

## Resource use

Nothing is written to disk for good, and every buffer has a cap:

| Where | What | Bound |
|---|---|---|
| HA memory | remuxed segments (a 10 s segment is 3.5-6.5 MB here) | `SEGMENT_CACHE_BYTES` = 96 MB, LRU |
| HA memory | playback sessions (segment plan + playlist text) | 64 sessions, 4 h idle TTL, 24 h window |
| HA memory | downloads being remuxed | 4 at a time (`MAX_PARALLEL_FETCHES`) |
| HA memory | queued segment fetches | cancelled once every client that asked has gone (hls.js aborts on each seek), unless already downloading |
| HA `/tmp` | one scratch file per remux (ffmpeg needs a seekable input) | deleted when the remux ends; `ss_vod_*.mp4` left by a crash are swept at startup |
| Browser memory | hls.js buffers | master 30 s ahead + 30 s behind, others 12 + 10 s; about 90 MB for a 4-camera grid |
| Browser disk | segments | none: served `Cache-Control: no-store` (the URLs are per-session, so a cached copy would never be used again) |

On the NAS, every segment download adds a line to the Surveillance Station
log (an hour of a 4-camera grid is ~1400 lines); SS's own log retention
setting bounds it.

## Security model

The playlist and segment URLs carry a random 256-bit token and need no HA auth
header, because hls.js cannot add one. The token is issued only to an
authenticated WebSocket client (`surveillance_station/vod`). It is scoped to one
camera and one time window, and expires 4 hours after its last use (at most
64 sessions, least recently used evicted first). DSM credentials stay in the
config entry. Login is a POST, and errors are rebuilt without request URLs, so
neither the password nor the SS session id reaches logs or browsers.

## Surveillance Station API notes (verified on SS 9.x, DSM 7)

- `SYNO.SurveillanceStation.Event` `List` v5 returns per-recording
  `startTime`/`stopTime`/`recording` (in progress)/`mountId`/`videoCodec`.
- **`fromTime`/`toTime` filter on a recording's start time, not overlap**, for
  both `Event.List` and `Recording.List`. A 5-minute window in the middle of a
  30-minute file returns nothing. Queries reach back 4 h and filter by overlap.
- Bookmarks are embedded in `Recording.List` **v5** results (`bookmark[]`, with
  `timestamp`/`endtime`). There is no list method on `Recording.Bookmark`.
- `Recording.Download` v6 with `offsetTimeMs` + `playTimeMs` returns an MP4 of
  that range. The start is floored to the keyframe (1 s GOP here: offsets of
  20.0, 20.5 and 20.9 s give identical cuts), and the end runs past the request
  (3 s asked → 4.9 s). It works on the file still being recorded, and puts moov
  at the end with no Range support. Errors come back as JSON with HTTP 200.
  Planning therefore uses whole-second boundaries, and ffmpeg `-t` trims the tail.

## ffmpeg / browser traps found while building this

- `-output_ts_offset` only reaches the fragments' `tfdt` with
  `-movflags +delay_moov+frag_discont`. With `empty_moov`, or without
  `frag_discont`, every output restarts at tfdt=0 and the offset hides in an
  edit list that MSE ignores. Every segment would then play at position 0.
- HA 2026.x swaps `window.customElements` for a scoped-registry polyfill while
  its app bundle loads. An `add_extra_js_url` module can run first, and a card
  defined then is invisible ("Custom element doesn't exist"). The card
  therefore waits for `<home-assistant>` to be defined, then registers.
- **Load the card as a Lovelace resource, not via `add_extra_js_url`.** The
  extra-JS import is baked into index.html. HA's service worker serves that
  page stale-while-revalidate, and in the Android app it kept serving a copy
  from before the integration was installed, even after reopening. The result
  was "Configuration error". The integration therefore registers (and
  version-bumps) its own Lovelace resource, because the resource list comes
  over the WebSocket on every dashboard load. It uses `add_extra_js_url` only
  when resources are in YAML mode.
- The browser must decode HEVC itself. Chrome/Edge with hardware decode,
  Safari and the HA Android/iOS apps generally can. Firefox and headless
  Chromium cannot. The card says so when `MediaSource` reports no `hvc1`.

## Tests

```
scripts/test.sh            # all tests, in a Python 3.14 container
scripts/test.sh -k reauth  # extra pytest arguments
```

HA 2026.9 needs Python 3.14, so the tests run in a throwaway container (the
venv is kept in the docker volume `ss-playback-test-venv`). The integration
tests use `pytest-homeassistant-custom-component` pinned to the HA release,
and mock only the library client: config flow (every step and error),
setup / unload / reauth trigger / unique-ID migration, the WebSocket commands,
the HLS views (token = credential, `no-store`, one NAS fetch per segment),
and diagnostics redaction.

The library's unit tests cover the planning/playlist logic. The endpoints and
the card were verified against a live HA 2026.9 + SS setup:
- ffprobe read the playlists: timestamps were continuous across recording
  rollovers, and a live playlist grew with its published prefix unchanged.
- The burned-in camera clock (OSD) matched the requested wall time.
- A Playwright run of the card played real footage (segments transcoded to
  H.264 in the test harness only, because headless Chromium has no HEVC). It
  covered URL-parameter start, in-window seeking, and 45 s of live playback.
