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
| `api.py` | Async SS Web API client: login (session renewal on 105/106/107/119), cameras, recordings, bookmarks, `Recording.Download` range cuts |
| `vod.py` | Pure logic: plans 10 s wall-clock-aligned HLS segments over a window, renders the playlist, splits ffmpeg output into init/media parts |
| `views.py` | HLS VOD endpoints `/api/surveillance_station/vod/<token>/…` with on-demand fetch, in-flight dedupe and a small LRU |
| `websocket.py` | `surveillance_station/cameras`, `/recordings`, `/bookmarks`, `/vod` |
| `frontend/ss-timeline-card.js` | `custom:ss-timeline-card`, auto-loaded by the integration. Uses hls.js 1.7.3 (Apache-2.0, vendored) |

## Install

1. Create a dedicated DSM user for HA. It needs Surveillance Station access
   (a viewer-level SS privilege profile that can play back and download
   recordings) and nothing else.
2. Copy `custom_components/surveillance_station` into HA's `config/custom_components/`
   (`HA_CONFIG=/path/to/config scripts/deploy.sh`) and restart HA.
3. Settings → Devices & services → Add → *Surveillance Station Playback*: host,
   port (5000 http / 5001 https), the DSM user.
4. Add the card to a dashboard (`scripts/dashboard.py` creates a test dashboard
   `/ss-playback` from `dashboards/ss-playback.json`).

```yaml
type: custom:ss-timeline-card
camera: Drive Way     # optional: name or id to start on
span: 3600            # optional: timeline width in seconds
```

URL parameters override on load: `?ss_camera=<name|id>&ss_time=<epoch seconds>`.
A notification can link straight to a moment this way.

## Security model

The playlist and segment URLs carry a random 256-bit token and need no HA auth
header, because hls.js cannot add one. The token is issued only to an
authenticated WebSocket client (`surveillance_station/vod`). It is scoped to one
camera and one time window, and expires after 4 hours (at most 64 live
sessions). DSM credentials stay in the config entry. Browsers never see them or
the SS session id.

## Surveillance Station API notes (verified on SS 9.x, DSM 7)

- `SYNO.SurveillanceStation.Event` `List` v5 returns per-recording
  `startTime`/`stopTime`/`recording` (in progress)/`mountId`/`videoCodec`.
- **`fromTime`/`toTime` filter on a recording's start time, not overlap**, for
  both `Event.List` and `Recording.List`. A 5-minute window in the middle of a
  30-minute file returns nothing. Queries reach back 4 h and filter by overlap.
- Bookmarks are embedded in `Recording.List` **v5** results (`bookmark[]`, with
  `timestamp`/`endtime`). There is no list method on `Recording.Bookmark`.
- `Recording.Download` v6 with `offsetTimeMs` + `playTimeMs` returns an MP4 of
  that range, rounded out to keyframes (10 s asked → ~11–12 s). It is
  second-accurate, works on the file still being recorded, and puts moov at the
  end with no Range support. Errors come back as JSON with HTTP 200.

## ffmpeg / browser traps found while building this

- `-output_ts_offset` only reaches the fragments' `tfdt` with
  `-movflags +delay_moov+frag_discont`. With `empty_moov`, or without
  `frag_discont`, every output restarts at tfdt=0 and the offset hides in an
  edit list that MSE ignores. Every segment would then play at position 0.
- HA 2026.x swaps `window.customElements` for a scoped-registry polyfill while
  its app bundle loads. An `add_extra_js_url` module can run first, and a card
  defined then is invisible ("Custom element doesn't exist"). The card
  therefore waits for `<home-assistant>` to be defined, then registers.
- The browser must decode HEVC itself. Chrome/Edge with hardware decode,
  Safari and the HA Android/iOS apps generally can. Firefox and headless
  Chromium cannot. The card says so when `MediaSource` reports no `hvc1`.

## Tests

```
python3 -m unittest discover -s tests -v
```
