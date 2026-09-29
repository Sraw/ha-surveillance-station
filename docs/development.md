[← README](../README.md)

# Development

## Layout

| Part | What it does |
|---|---|
| `synology_ss/` | The protocol library **`synology-ss-playback`** (no HA imports, own `pyproject.toml` and tests; published on [PyPI](https://pypi.org/project/synology-ss-playback/)): the SS Web API client (session renewal on 105/106/107/119, SS info, cameras, recordings, bookmarks, `Recording.Download` range cuts, time-lapse files), the 10 s segment planner and playlist renderer, `fetch_segment` (download + ffmpeg remux + fMP4 split), and for time-lapse the day planner (`plan_day`) and `fetch_timelapse_segment` (streamed download + transcode) |
| `custom_components/surveillance_station/` | The integration, a thin layer over the library: config flow (user / reauth / reconfigure, unique ID = NAS serial), `entry.runtime_data` = the logged-in client, diagnostics (including the Frigate bridge: subscribed?, review messages (several per review) and how each ended — ignored by reason, dropped, failed, or bookmarked then announced / not announced — queue, failing now, last error) |
| `…/errors.py` | The WebSocket commands' own refusals (entry not loaded, session gone: `not_found`; bad input: `invalid_format`), so that any other error reaches HA's handler and is logged |
| `…/views.py` | The HTTP views: the stream relay `/api/surveillance_station/live/<token>` (live and recordings); HLS VOD endpoints `/api/surveillance_station/vod/<token>/…` for browsers without MSE; event thumbnails `/api/surveillance_station/thumbnail/…` |
| `…/manager.py` | `VodManager`, what the views, the WebSocket commands and the Frigate bridge ask for playback: sessions, the GPU check, the time-lapse file list, bookmark frames, whether each entry's SS answers (a Repairs issue if an outage lasts); the parts below do the rest |
| `…/segments.py` | Playback sessions (`VodSession`) and their segments: fetched on demand, one job per segment, in a byte-bounded cache, behind the fetch queue |
| `…/thumbnails.py` | Event thumbnails and notification images: one job per frame, kept in memory and on disk (`thumbnail_store.py`) |
| `…/tokens.py` | Signed image URLs and single-use live-stream tokens (what stands in for HA's auth where a browser can't send it) |
| `…/bookmarks.py` | The bookmark list, fetched once for everyone (cached 60 s) and indexed once per fetch (by camera, kinds and start) for the event list's pages and kind chips; what a bookmark's name says was seen |
| `…/shared.py` | What the caches share: one job per key whose result every waiting request shares (optionally cancelled once all of them left), and an LRU map bounded by bytes |
| `…/frigate.py` | Optional: Frigate review items (MQTT) as SS bookmarks, and a `surveillance_station_detection` event per new one (see *Frigate detections*); with a Frigate URL, the notification image and the Frigate bookmarks' thumbnails (Frigate's snapshots, `/api/surveillance_station/frigate_image/…`) |
| `…/frigate_queue.py` | The Frigate review messages waiting for SS, one per review (fresh, replayed, deferred), kept across restarts for a day |
| `…/websocket.py` | `surveillance_station/cameras`, `/recordings`, `/bookmarks` (a time range, for the timeline), `/bookmark_page` (newest first, cursor-paged, for the event list), `/live` (a single-use URL for a camera's stream: live, or the recordings from a time), `/vod`, `/vod_runs` (HLS, for browsers without MSE), `/timelapse_days`, `/timelapse` (a time-lapse session: one camera, one day) |
| `…/frontend/ss-timeline-card.js` | `custom:ss-timeline-card`, registered by the integration as a Lovelace resource. No dependencies |
| `…/frontend/ss-timelapse-card.js` | `custom:ss-timelapse-card`, loaded by the timeline card (same version, no resource of its own) |
| `…/mute.py`, `…/mute_services.py`, `…/switch.py` | Muting notifications (see *Frigate detections*): the rules and what they cover (`MuteRules.coverage`, which the switches show), the mute/unmute actions and the notification's buttons, the switches |
| `…/frontend/ss-mute-card.js` | `custom:ss-mute-card`, loaded by the timeline card (same version, no resource of its own): the mute switches as a hierarchy (everything, a kind, a camera, one kind on a camera), found as this integration's switches with `camera` and `locked` attributes; `tests/browser/mute-card.mjs` drives it |
| `…/frontend/ss-common.js` | What the cards share: MSE and the MP4 init-segment parsers, SS's stream message parser, the timeline ticks, the veil, control labels, per-viewer preferences. Each card imports it with its own `?v=` version query, so a release never mixes a new card with a stale cached copy |

## Deploying a checkout

`HA_CONFIG=/path/to/config [HA_CONTAINER=homeassistant] scripts/deploy.sh`,
then restart HA. It copies the integration into `custom_components/` and
installs the library from this checkout into `<config>/deps` (the user site
the HA container puts on `sys.path`), so a change to both can be tried before
the library is released. That install survives HA image updates; after an
update to a newer Python, run it again. To take it out again:
`docker exec -e PYTHONUSERBASE=/config/deps homeassistant pip uninstall -y
synology-ss-playback`.

`scripts/dashboard.py` creates a test dashboard `/ss-playback` from
`dashboards/ss-playback.json`.

The cards are served to be kept for a month (every URL of them carries the
version), so a changed card deployed under the same version shows only after
a reload that bypasses the browser's cache, or once `manifest.json`'s
`version` and `CARD_VERSION` are bumped.

## Releasing

1. Library changed: bump `synology_ss/pyproject.toml`, push it to `main`, then tag
   `synology-ss-vX.Y.Z` and push the tag: `.github/workflows/publish-library.yml`
   runs the library tests, builds it and uploads it to PyPI (Trusted Publishing,
   no token; the tag must match the version and be on a commit already on
   `main`). Then pin the new version in `manifest.json`'s
   `requirements`. (Manual fallback: `python -m build synology_ss` + `twine upload`.)
   Set required reviewers on the repository's `pypi` environment (Settings →
   Environments → pypi) so the upload waits for an approval.
2. Bump `manifest.json`'s `version`, tag `vX.Y.Z` and create a GitHub release
   (HACS offers releases).

## Tests

```
scripts/test.sh            # all tests, in a Python 3.14 container
scripts/test.sh -k reauth  # extra pytest arguments
node --test "tests/js/*.test.mjs"   # the cards' pure helpers (Node 22+, nothing to install)
```

`.github/workflows/ci.yml` runs the same suite on push to `main` and on pull
request (plain Python 3.14 via `actions/setup-python`, not the local
container - a GitHub-hosted runner doesn't need the memory/time cap
`scripts/test.sh` uses to protect the dev host), plus `hassfest` and the HACS
integration check (both pinned to a commit), and for the cards (job `cards`)
`node --check` on each file and the `tests/js` tests: codec strings from real-shaped init
segments (a wrong one fails `addSourceBuffer`), stream messages, DST-day
ticks, the slider keys, preferences. Coverage must clear 95% separately for
`custom_components/surveillance_station` and for
`synology_ss/src/synology_ss_playback` (`coverage report --include=... --fail-under=95`,
once per package) - a single pooled number could hide one package dragging
the other up. `scripts/test.sh` with no extra arguments runs the same two
checks after the full suite; a filtered run (`-k foo`) skips them, since it
only exercises a slice of the code and the gate would fail regardless of
whether the selected tests pass.

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
- Playwright drove the card in Google Chrome decoding the real H.265 through
  VA-API (Chrome has no software HEVC decoder on Linux; it runs headful in a
  container on a headless Weston with the Intel GPU's render node, since
  `--headless` can't use hardware decode): `tests/browser/run.sh <scenario>.mjs`
  against a running HA, with the card served from the checkout (see the
  script's header; `npm install` in `tests/browser` first). Every scenario
  prints `FAIL` for a check that failed and exits 1. Covered: live; jumps (buffered,
  over the socket, from live); a recording-file boundary; 1/2/4/8x; pause
  (SS paused by flow control) and resume; the 4-camera grid in step after
  jumps, speed changes, pause and a leader change; a follower's 72-minute gap
  and a gap the leader lands in; gaps all cameras share; a 29 s hole inside a
  recording file (bridged in ~1.5 s); sound kept within ~0.1 s of the video;
  the no-MSE path (native HLS) on a phone-sized viewport; the live timeline
  scrolling with the present; races (a follower made leader while it holds
  its first frame; Live, a past time, Live again before anything landed);
  notification deep links; grid / one-camera modes and the overlay menu;
  the event list beside and under the video, kind chips, smart search;
  audio the browser refuses; the time-lapse card (first frame, seeks, veil,
  zoom, pan and skips, also on a phone).

## Surveillance Station API notes (verified on SS 9.x, DSM 7)

- Time-lapse (undocumented; what SS's UI calls):
  `SYNO.SurveillanceStation.TimeLapse.Recording` `List` v1 (`lapseId` -1 = all
  tasks, `start`/`limit` ≤ 100) lists the files with `startTime`,
  `rangeMinute` (wall time covered so far), `frameCount` (30 fps),
  `imgWidth`/`imgHeight`, `video_type` (6 = H.265), `recording` (still being
  written); its `fromTime`/`toTime` match a file's start only.
  `Recording.Download` v6 with `recEvtType=3` cuts a file by video time: the
  cut starts on exactly the frame asked for (all frames are keyframes) and
  holds the whole seconds asked for plus 29 frames. The SS stream socket
  ignores `recEvtType` (it plays recordings only).

- `SYNO.SurveillanceStation.Event` `List` v5 returns per-recording
  `startTime`/`stopTime`/`recording` (in progress)/`mountId`/`videoCodec`.
- **`fromTime`/`toTime` filter on a recording's start time, not overlap**, for
  both `Event.List` and `Recording.List`. A 5-minute window in the middle of a
  30-minute file returns nothing. Queries reach back 4 h and filter by overlap.
- A recording in progress has its `stopTime` moved forward about every 10 s
  (0-10 s behind now). HA reports one whose end is over 15 s old as no longer
  in progress, whatever `recording` still says.
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
  before the time, 0.07 s after connecting, then sends in real time (no burst
  up to the time asked for). Over the open socket: `time=<epoch>` jumps
  (the old place keeps coming for a moment; a new recording file, reached by
  playing or by a jump, resends the header, `ftyp` and `moov`, with the
  timestamps starting over, while a jump inside the same file doesn't),
  `pause=true` stops sending (a `time=` while paused sends just the first two
  frames of the new place), `pause=false` carries on, `speed=N` sends N
  seconds of footage a second with the sample durations divided by N (at 8x
  some frames are left out; 16x skips ~0.8 s at a time) and survives jumps.
  Paused 15 min with keep-alives, the socket stayed open and carried on. Gaps are skipped
  over, a time before the oldest recording starts at the oldest, and a time
  in the future gives the real-time stream. Playback started in the recent
  past never gets there at 1x: SS sends it at the pace of real time, so it
  stays that far behind (the card shows no LIVE; the Live button jumps).
- `ThirdParty/SnapShot/Take` documents `time=` (ISO 8601) but can't take a
  past frame: with an offset or `Z` every time answers 400 (a minute ago as
  well as days); a bare local time is read an hour late in DST, so a recent
  one lands in the future and gets the live frame (the burned-in clock shows
  the moment of the request; two times 9 minutes apart returned the same
  bytes), and anything older than that hour is 400 again. So thumbnails come
  from `Recording.Download` + ffmpeg. `ThirdParty/Recording/Download` returns a
  zip, whole seconds only, so segments use `Recording.Download` v6 (ms offsets).
- `Info.GetInfo` v8 can answer success **without** `serial` (seen once, right
  after an HA restart; fine again moments later). The library raises `SSError`
  for that, so setup is retried (`ConfigEntryNotReady`) instead of the entry
  being left in `setup_error`, which silently stops bookmarks, notifications
  and playback. A missing `timezoneTZDB` means UTC, but is asked again on the
  next bookmark call rather than cached.
- Bookmarks: the documented `ThirdParty.Bookmark.List` v1 ([SS 9.3 Web API
  reference](https://surveillance-api.synology.com/)) returns every bookmark of
  the given `camIds`, newest first, times as **NAS-local ISO strings without
  an offset**; `Info.GetInfo` gives the zone (`timezoneTZDB`). Its
  `startTime`/`endTime` filters are unusable: they act at day granularity with
  the boundaries in the wrong place (a 14:25-15:00 window on a day with
  bookmarks at 14:28 and 14:47 returns none). So the integration fetches the
  full list (cached 60 s, dropped at once when HA makes or changes a
  bookmark) and filters and pages it itself. The undocumented
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

- Motion / object events (looked into, not used; SS 9.3, 2026-09-25):
  `SYNO.SurveillanceStation.Event.List` filters by recording reason (2
  motion, 3 alarm, 9 action rule, 10 advanced continuous...), so cameras
  that SS records *on motion* have events with start and end that a
  timeline could draw. Under continuous recording there are none: each
  30-minute file only carries a `trigger_label` bitmask (0x101: motion
  somewhere in it), and the response's `reason`/`mode` (0 / 5 for
  continuous) don't match the request's documented codes. SS's own timeline
  marks come from undocumented calls. Object detection needs a DVA model;
  camera-side person/vehicle detection isn't exposed (HA's Reolink
  integration reads it from the camera). Not built: untestable here, where
  every camera records continuously and Frigate's bookmarks are the events.

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
  There is deliberately no transcoding to H.264: HA would have to decode 4K
  HEVC per viewer (several CPU cores each without a GPU in its container),
  and SS's WebSocket stream has no documented way to pick a camera's H.264
  sub stream. Cameras SS records as H.264 pass through the same way as
  HEVC ones (no `hvc1` tag), but have not been tried against a real SS.

## History

Before 0.7 live was that growing playlist, 20 s behind. On Wi-Fi cameras it
buffered often: SS reports a live recording's end as the last data it wrote,
which lags on Wi-Fi, so the playlist grew in late, uneven steps (a 10 s slot
arrived as 8 + 2 or 3 + 7 s) and the few seconds buffered ran out. Before 0.8
recordings were HLS everywhere, played by hls.js: a seek waited for a 10 s
segment to be downloaded from SS and remuxed by HA (1-3 s), grid followers
were kept in step by seeking into those segments.
