# Surveillance Station Playback for Home Assistant

Play back **Synology Surveillance Station** recordings inside Home Assistant,
with a scrubbable wall-clock timeline and SS bookmarks drawn on it. SS keeps
recording, archiving and doing timelapse. This integration only reads from it.
Video is never transcoded: SS cuts the requested range out of its own
recording, HA stream-copies it into fragmented MP4, and the browser decodes the
original H.265/H.264.

Status: step 1 of a larger plan (integration + test card). Next come an
Advanced Camera Card engine, notification deep links, and Frigate detections
written as SS bookmarks through the documented `ThirdParty.Bookmark.Create`.

## Pieces

| Part | What it does |
|---|---|
| `synology_ss/` | The protocol library **`synology-ss-playback`** (no HA imports, own `pyproject.toml` and tests; ready for PyPI, not published yet): the SS Web API client (session renewal on 105/106/107/119, SS info, cameras, recordings, bookmarks, `Recording.Download` range cuts), the 10 s segment planner and playlist renderer, and `fetch_segment` (download + ffmpeg remux + fMP4 split) |
| `custom_components/surveillance_station/` | The integration, a thin layer over the library: config flow (user / reauth / reconfigure, unique ID = NAS serial), `entry.runtime_data` = the logged-in client, diagnostics |
| `…/views.py` | HLS VOD endpoints `/api/surveillance_station/vod/<token>/…`: playback sessions, the byte-bounded segment cache, the fetch queue; the bookmark cache; event thumbnails `/api/surveillance_station/thumbnail/…`; the live-stream relay `/api/surveillance_station/live/<token>` |
| `…/websocket.py` | `surveillance_station/cameras`, `/recordings`, `/bookmarks` (a time range, for the timeline), `/bookmark_page` (newest first, cursor-paged, for the event list), `/live` (a single-use real-time stream URL), `/vod`, `/vod_runs` |
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

To remove it: take the card off your dashboards and delete the entry under
Settings → Devices & services (deleting the last entry also removes the card's
Lovelace resource). Then delete `custom_components/surveillance_station`, run
`docker exec -e PYTHONUSERBASE=/config/deps homeassistant pip uninstall -y
synology-ss-playback`, and restart HA.

```yaml
type: custom:ss-timeline-card
cameras: [Drive Way, Front Door]   # optional: cameras shown (default: all)
camera: Drive Way     # optional: the one to start on
span: 3600            # optional: timeline width in seconds (15m .. 7d)
clock: false          # optional: hide the date/time overlay
```

- **Opens playing**: live (about 1 s behind real time, marked LIVE), or the
  moment a link asked for. Coming back to the view resumes live if it was live.
- **Fits the screen**: from the camera chips to the timeline, the card sizes
  the video so it all fits in the window (cells stay 16:9; the grid gets
  narrower when full width would be too tall). Narrow cards (phones) get one
  compact row of controls: the ±30 s buttons and the time labels are dropped.

- **Cameras shown**: one camera is the single view, several are a grid
  (2 side by side, 3-4 in 2x2, more in 3 columns). The camera chips add or
  remove one (tinted = shown, a ring in the camera's colour = the master; the
  last one stays). Each camera has a colour, used for its chip, its cell
  label, its timeline pins and its event rows. The
  square button shows just the master, and switches back to the previous set.
  Double-tapping a grid cell does the same for that camera. The chips' choice
  is remembered per browser (`localStorage`) and wins over `cameras`.
- **Master**: tap a cell. It has the sound and the clock, and the others
  follow it every 500 ms: a drift over 2 s (more at 4x/8x) is a seek, a smaller
  one over 0.1 s nudges `playbackRate` by up to ±20 %. A camera with no
  recording at that time pauses under a "No recording" veil. Seek / skip /
  Live apply to all cameras.
- **Timeline and Events cover the cameras shown**: recording bars are the time
  where any of them recorded, and the bookmarks on the timeline and in the
  list come from the same list, so they always agree. To see another camera's
  events, show it. Timeline spans: 15 min to 7 days.
- **Bookmark pins** on the timeline open their event when tapped, exactly like
  tapping the event in the list.
- **Clock**: the overlay button toggles it; remembered per browser.
- **Fullscreen**: the stage (video + a slim auto-hiding control bar) goes
  fullscreen and asks for landscape (`screen.orientation.lock`; honoured on
  Android / the companion app, ignored where the browser doesn't allow it).
  On an iPhone, which has no element fullscreen, the master's own video player
  goes fullscreen instead.
- **Zoom**: pinch or double-tap (single view) zooms up to 8x, drag pans,
  double-tap resets; the mouse wheel zooms in fullscreen. At 1x vertical
  swipes still scroll the page (`touch-action: pan-y`).

The **Events** list is a collapsible sidebar on wide cards (≥ 1000 px) and
sits under the timeline on narrow ones. It holds every SS bookmark of the
cameras shown, newest first, grouped by day, each with a thumbnail of the
moment. It loads 30 at a time as you scroll (cursor-paged, so events created
meanwhile don't shift it). Once a minute it is brought up to date: new events
are merged in by time (an event can be bookmarked after a later one), events
deleted in SS go away, and after more than a page of new ones, or 12 h, it
starts over. Rows are kept across refreshes, so thumbnails aren't reloaded.
Tapping one
makes that camera the master and plays from 3 s before it; events under the
playhead are highlighted. SS's own motion detections are not exposed by any
documented API (in continuous mode every `Event` is a recording file), so
bookmarks are the event source.

URL parameters override on load: `?ss_camera=<name|id>&ss_time=<epoch seconds>`.
A notification can link straight to a moment this way.

**Live** is Surveillance Station's real-time stream, not a recording: SS's
documented WebSocket stream (`/ss_webstream_task/`, fragmented MP4), relayed
by HA and fed to the `<video>` through MSE. The card keeps 0.8 s buffered
ahead of the playhead as a jitter margin (Wi-Fi cameras deliver frames in
bursts), holding it by nudging the speed ±7-10 % and jumping if it falls more
than a few seconds behind, so live is about 1 s behind real time. Sound, when
unmuted, comes from the master only, through its own `<audio>` (a video that
waited for audio would stall on every burst). Pausing live holds the frame;
playing again catches up to now. Anything earlier than ~20 s ago (a skip back,
a timeline tap, an event) plays the recordings.

Recordings play as HLS. A window that reaches the recent past is an HLS EVENT
playlist that grows as SS records (segments are only published once they end
on the 10 s grid behind real time, so they never change afterwards); older
windows are closed VOD playlists. When one ends, the card looks up the next
recording and carries on.

Before 0.7 live was that growing playlist, 20 s behind. On Wi-Fi cameras it
buffered often: SS reports a live recording's end as the last data it wrote,
which lags on Wi-Fi, so the playlist grew in late, uneven steps (a 10 s slot
arrived as 8 + 2 or 3 + 7 s) and the few seconds buffered ran out.

## Resource use

Nothing is written to disk for good, and every buffer has a cap:

| Where | What | Bound |
|---|---|---|
| HA memory | remuxed segments (a 10 s segment is 3.5-6.5 MB here) | `SEGMENT_CACHE_BYTES` = 96 MB, LRU |
| HA memory | playback sessions (segment plan + playlist text) | 64 sessions, 4 h idle TTL, 24 h window |
| HA memory | downloads being remuxed | 4 at a time (`MAX_PARALLEL_FETCHES`) |
| HA memory | queued segment fetches | cancelled once every client that asked has gone (hls.js aborts on each seek), unless already downloading |
| HA `/tmp` | one scratch file per remux (ffmpeg needs a seekable input) | deleted when the remux ends; `ss_vod_*.mp4` left by a crash are swept at startup |
| HA | live relays | pass-through (a slow viewer slows the read from SS, nothing queues in HA); 16 at most |
| Browser memory | live stream | about 20 s behind the playhead, trimmed as it goes; a backlog of 90 fragments drops to the next keyframe |
| Browser memory | hls.js buffers | master 30 s ahead + 30 s behind, others 12 + 10 s; about 90 MB for a 4-camera grid |
| HA memory | event thumbnails (JPEG, 320 px, 10-20 KB) | 16 MB LRU (+256 B per entry, so "nothing recorded" answers count too; those expire after 5 min); 2 made at a time, one job per frame however many ask, cancelled once nobody waits for it |
| HA memory | the bookmark list of each entry | re-read after 15 s; one fetch at a time, whose result (or error, kept 5 s) every waiting request shares |
| Browser disk | thumbnails | `private, max-age=86400` (a thumbnail of a past moment never changes) |
| Browser disk | segments | none: served `Cache-Control: no-store` (the URLs are per-session, so a cached copy would never be used again) |

On the NAS, every segment download adds a line to the Surveillance Station
log (an hour of a 4-camera grid is ~1400 lines); SS's own log retention
setting bounds it.

## Security model

Live streams: `surveillance_station/live` returns a single-use URL (a random
256-bit token, valid 30 s) for one camera; HA opens SS's stream with its own
session and relays it, so the SS session id never reaches the browser. The
browser can only send keep-alives through it. At most 16 streams at once;
unloading the entry closes them.

The playlist and segment URLs carry a random 256-bit token and need no HA auth
header, because hls.js cannot add one. The token is issued only to an
authenticated WebSocket client (`surveillance_station/vod`). It is scoped to one
camera and one time window, and expires 4 hours after its last use (at most
64 sessions, least recently used evicted first). DSM credentials stay in the
config entry. Login is a POST, and errors are rebuilt without request URLs, so
neither the password nor the SS session id reaches logs or browsers.

Event thumbnail URLs (`/api/surveillance_station/thumbnail/…`) work the same
way, since an `<img>` can't send a header either: the WebSocket hands out URLs
carrying an expiry (about 24 h) and an HMAC-SHA256 of the path and expiry,
under a key made at startup. Each opens one JPEG of one camera at one moment.
Anything unsigned, tampered with or expired is a **404**. HA's own signed
paths (`async_sign_path`) were used at first and dropped: HA answers a stale
one (after every HA restart, or after a day) with 401, and counts every 401 as
a failed login, so a wall tablet left open would get its IP banned under
`login_attempts_threshold`.

Every HA user can use the card and so see every camera, like HA's own camera
entities; there is no per-user camera permission.

## Surveillance Station API notes (verified on SS 9.x, DSM 7)

- `SYNO.SurveillanceStation.Event` `List` v5 returns per-recording
  `startTime`/`stopTime`/`recording` (in progress)/`mountId`/`videoCodec`.
- **`fromTime`/`toTime` filter on a recording's start time, not overlap**, for
  both `Event.List` and `Recording.List`. A 5-minute window in the middle of a
  30-minute file returns nothing. Queries reach back 4 h and filter by overlap.
- Real-time stream: `ws(s)://<nas>/ss_webstream_task/?camId=<id>&_sid=<sid>`
  (documented on the API reference's "Liveview / Playback" page). Messages: a
  4-byte big-endian header end, a query-string header, then fMP4. The first
  says `vdoCodec=H265&adoCodec=MPEG4-GENERIC`, then per stream `ftyp`, `moov`
  (sample entry `hev1`) and one `moof`+`mdat` per frame, with `mediaType`
  (1 video, 2 audio), `key` and `msec` (the frame's epoch ms). A bad or expired
  sid gets the socket closed at once, with nothing sent. Measured here: first
  frame 0.13 s after connecting, frames ~0.1 s behind real time.
- The same socket plays recordings with `&time=`. **A bare local time is read
  one hour off during DST** (`2026-09-24T16:40:58` started at 17:40:58);
  with an offset (`…-07:00`), `…Z` or epoch seconds it starts at the keyframe
  before the time, 0.07 s after connecting. `speed=4` plays at 4x. Not used
  yet: recordings still play as HLS.
- `ThirdParty/SnapShot/Take` ignores `time=` for recent or future times (it
  returns the live frame) and fails (400) for older ones, so thumbnails come
  from `Recording.Download` + ffmpeg. `ThirdParty/Recording/Download` returns a
  zip, whole seconds only, so segments use `Recording.Download` v6 (ms offsets).
- Bookmarks: the documented `ThirdParty.Bookmark.List` v1 ([SS 9.3 Web API
  reference](https://surveillance-api.synology.com/)) returns every bookmark of
  the given `camIds`, newest first, times as **NAS-local ISO strings without
  an offset**; `Info.GetInfo` gives the zone (`timezoneTZDB`). Its
  `startTime`/`endTime` filters are unusable: they act at day granularity with
  the boundaries in the wrong place (a 14:25-15:00 window on a day with
  bookmarks at 14:28 and 14:47 returns none). So the integration fetches the
  full list (cached 15 s) and filters and pages it itself. The undocumented
  `Recording.Bookmark.ListBookmark` v1 does page properly (`start`/`limit`),
  but isn't used.
- A thumbnail is `Recording.Download` of 1.5 s at the moment plus one ffmpeg
  frame, scaled to 320 px (~0.5 s, 10-20 KB).
- `Recording.Download` v6 with `offsetTimeMs` + `playTimeMs` returns an MP4 of
  that range. The start is floored to the keyframe (1 s GOP here: offsets of
  20.0, 20.5 and 20.9 s give identical cuts), and the end runs past the request
  (3 s asked → 4.9 s). It works on the file still being recorded, and puts moov
  at the end with no Range support. Errors come back as JSON with HTTP 200.
  Planning therefore uses whole-second boundaries, and ffmpeg `-t` trims the tail.

## Known limitations

- A bookmark in the hour repeated when DST ends is placed in the first of the
  two (SS lists bookmark times as local times without an offset).
- The timeline re-lists every shown camera's recordings for the whole span
  once a minute while it follows the present; at 7 d that is a few `Event.List`
  pages per camera.
- A card that must fit a short screen (a phone in landscape, 4 cameras) gets
  small cells: the chips, controls and timeline need about 230 px. Fullscreen
  is the way to watch there.
- The event list's once-a-minute refresh reads the newest page. A bookmark
  created now for a moment older than the newest 30 shows up when the list
  starts over (after 12 h, a camera change, or a reload).
- After HA restarts, HA rebuilds the dashboard, so the card starts over (live)
  instead of resuming a paused moment.

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
