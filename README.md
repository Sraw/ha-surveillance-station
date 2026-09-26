# Surveillance Station Playback for Home Assistant

Play back **Synology Surveillance Station** recordings inside Home Assistant,
with a scrubbable wall-clock timeline and SS bookmarks drawn on it. SS keeps
recording, archiving and doing timelapse. This integration only reads from it,
except for one optional write: Frigate detections become SS bookmarks.
Video is never transcoded: SS cuts the requested range out of its own
recording, HA stream-copies it into fragmented MP4, and the browser decodes the
original H.265/H.264.

Status: playback, the card, notification links, and Frigate detections as SS
bookmarks (with an event to notify from).

## Pieces

| Part | What it does |
|---|---|
| `synology_ss/` | The protocol library **`synology-ss-playback`** (no HA imports, own `pyproject.toml` and tests; ready for PyPI, not published yet): the SS Web API client (session renewal on 105/106/107/119, SS info, cameras, recordings, bookmarks, `Recording.Download` range cuts), the 10 s segment planner and playlist renderer, and `fetch_segment` (download + ffmpeg remux + fMP4 split) |
| `custom_components/surveillance_station/` | The integration, a thin layer over the library: config flow (user / reauth / reconfigure, unique ID = NAS serial), `entry.runtime_data` = the logged-in client, diagnostics (including the Frigate bridge: subscribed?, review messages (several per review) and how each ended — ignored by reason, dropped, failed, or bookmarked then announced / not announced — queue, failing now, last error) |
| `…/views.py` | The stream relay `/api/surveillance_station/live/<token>` (live and recordings); HLS VOD endpoints `/api/surveillance_station/vod/<token>/…` for browsers without MSE: playback sessions, the byte-bounded segment cache, the fetch queue; the bookmark cache; event thumbnails `/api/surveillance_station/thumbnail/…` |
| `…/frigate.py` | Optional: Frigate review items (MQTT) as SS bookmarks, and a `surveillance_station_detection` event per new one (see *Frigate detections*) |
| `…/websocket.py` | `surveillance_station/cameras`, `/recordings`, `/bookmarks` (a time range, for the timeline), `/bookmark_page` (newest first, cursor-paged, for the event list), `/live` (a single-use URL for a camera's stream: live, or the recordings from a time), `/vod`, `/vod_runs` (HLS, for browsers without MSE) |
| `…/frontend/ss-timeline-card.js` | `custom:ss-timeline-card`, registered by the integration as a Lovelace resource. No dependencies |

## Requirements

- **Surveillance Station 9 on DSM 7** (tested: SS 9.3, DSM 7.2). Setup checks
  that the NAS has the Web APIs used and says so if not.
- **Home Assistant 2026.9** or newer, with `ffmpeg` (HA's own image has it).
- A dedicated DSM account without two-step verification (setup says so if
  DSM asks it for a code; exempt it from a policy that enforces 2FA).
- For playback in the browser: H.265 decoding if the cameras record H.265
  (see *Known limitations*); sound plays in whatever codec the camera sends
  that the browser can play (AAC everywhere).
- NAS, HA and the viewing devices on NTP: the card lines up SS's frame times
  with the device's clock, and "live" means within a few seconds of it.
- Cameras with a **1-second I-frame interval** (GOP = frame rate) for exact
  seeks and notification frames; SS cuts at keyframes, so a longer GOP
  makes both up to one GOP early.

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
cameras: [Drive Way, Front Door]   # optional: the grid's cameras (default: all)
camera: Drive Way     # optional: the one to start on
view: single          # optional: start on one camera rather than the grid
span: 3600            # optional: timeline width in seconds (15m .. 7d)
clock: false          # optional: hide the date/time on the video
live_badge: false     # optional: hide the LIVE badge on the video
camera_names: false   # optional: hide the camera names in grid cells
```

The eye button in the controls hides or shows each of those three; the
viewer's choice is remembered in the browser and wins over the options.

- **Opens playing**: live (about 1 s behind real time, marked LIVE), or the
  moment a link asked for. Coming back to the view resumes live if it was live.
- **Fits the screen**: from the camera chips to the timeline, the card sizes
  the video so it all fits in the window (cells stay 16:9; the grid gets
  narrower when full width would be too tall). Narrow cards (phones) get one
  compact row of controls: the ±30 s buttons and the time labels are dropped.

- **Grid or one camera**: the square / grid button in the controls switches
  between the two. In the grid (2 side by side, 3-4 in 2x2, more in 3
  columns) the camera chips add or remove a camera (tinted = shown, a ring
  in the camera's colour = the master; the last one stays). With one camera
  the chips switch which one, and the grid keeps its cameras for when it
  comes back. Double-tapping a grid cell shows that camera alone. Each camera
  has a colour, used for its chip, its cell label, its timeline pins and its
  event rows. The mode, the grid's cameras and the camera watched are
  remembered per browser (`localStorage`) and win over `view` / `cameras` /
  `camera`.
- **Master**: tap a cell. It has the sound and the clock, and the others
  follow its wall-clock time (see *Grid* below). A camera with no recording
  at that time holds under a "No recording" veil. Seek / skip / Live apply to
  all cameras.
- **Timeline and Events cover the cameras shown**: recording bars are the time
  where any of them recorded, and the bookmarks on the timeline and in the
  list come from the same list, so they always agree. To see another camera's
  events, show it. Timeline spans: 15 min to 7 days. Watching live, the
  timeline scrolls with the present (held still while the pointer is on it,
  and for 15 s after a pan).
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

- **Kinds**: chips above the list (Person, Car, Animal, …: the parts of the
  bookmarks' names, those of more than one bookmark, most common first)
  narrow the list and the timeline's pins to the kinds chosen; remembered per
  browser.
- **Smart search** (with Frigate, and its URL in the options): a search box
  above the list asks Frigate's semantic search, in words ("white car",
  "person with a box"; English, Frigate's CLIP model), or "similar to" a
  bookmark (the button on a Frigate bookmark's row: by the foremost object
  seen in it, image to image). Frigate finds; SS plays: each result is
  placed on the SS camera its Frigate camera maps to, at its start time, and
  only one on a Frigate bookmark of its kind on that camera then (the one it
  began in, else the one it overlaps most) is a result: that bookmark,
  listed once. What Frigate saw and let go (outside the zones that count, a
  kind not bookmarked) isn't an event, so it isn't found either. Matched
  by camera and time, not by Frigate's review ids: Frigate deletes reviews
  with its own recordings (days), but keeps tracked objects as long as their
  snapshots. Results come best first, each with Frigate's crop of the
  object, and the kind chips filter them too (a bookmarked one by its name).
  Tapping one plays SS's recording from 3 s before it. Frigate ranks the best 100 of all cameras before filtering by
  camera, so with a few cameras shown there may be fewer results.
  Needs `semantic_search.enabled` in Frigate; on a machine whose iGPU also
  decodes and detects, keep it on the CPU (`model_size: small`): with
  `large` on the iGPU, a burst of searches here hung the GPU, and the
  cameras' decoders came back crippled until Frigate restarted. Frigate
  (0.18) answers two semantic searches at once with nothing, so this
  integration asks them one at a time (and an empty answer once more).
  Frigate deletes a tracked object, and so what can be found, with its
  snapshot (`snapshots.retain`): keep that as long as SS keeps recordings.

URL parameters override on load: `?ss_camera=<name|id>&ss_time=<epoch seconds>`.
A notification can link straight to a moment this way.

**Video comes from Surveillance Station's own stream**, live and recorded
alike: SS's documented WebSocket stream (`/ss_webstream_task/`, fragmented
MP4 with each frame's wall-clock time), relayed by HA and fed to the `<video>`
through MSE (`ManagedMediaSource` on iOS 17.1+).

- **Live** keeps 0.8 s buffered ahead of the playhead as a jitter margin
  (Wi-Fi cameras deliver frames in bursts), holding it by nudging the speed
  ±7-10 % and jumping if it falls more than a few seconds behind, so live is
  about 1 s behind real time. Pausing live holds the frame; playing again
  catches up to now. Anything earlier than ~20 s ago (a skip back, a timeline
  tap, an event) plays the recordings.
- **Recordings** open the same stream at a time (`time=`, epoch seconds). SS
  starts at the keyframe before it, goes on across recording files and over
  gaps by itself, and sends the footage at the pace it plays (at 2-8x with the
  timestamps squeezed, so the `<video>` itself runs at about 1x). The card
  starts once 0.3 s is buffered and plays a little slower until 1 s is; when
  more than 4 s piles up (paused, or a decoder that can't keep up) it tells
  SS to pause. A jump within what's buffered since the last jump (up to the
  last ~20 s, and whatever arrived ahead) is instant; any other goes over the open socket (`time=`,
  about 0.5 s). A jump or tap lands on that keyframe, up to a GOP (~1 s here)
  before the time asked for.
- **Grid**: every camera has its own stream; the others follow the master's
  wall-clock time. SS never sends faster than asked, so a follower that is
  behind can't catch up by playing faster: it asks for 1 s (times the speed)
  past the master's time and holds its first frame until the master gets
  there (landing behind anyway, it asks again with twice the lead). A few
  tenths off are made up by ±20 % speed; a camera with nothing recorded holds
  its next footage behind a "No recording" veil until the master reaches it.
  Measured on a 4-camera grid: in step to ~0.15 s, 1-2 s after a jump.
- **Sound**, when unmuted, comes from the master only (live, or recordings at
  1x), through its own `<audio>` kept on the video's wall-clock time (a video
  that waited for audio would stall on every burst).

Browsers without MSE (Safari before 17.1) play recordings as HLS instead: a
window that reaches the recent past is an HLS EVENT playlist that grows as SS
records (segments are only published once they end on the 10 s grid behind
real time, so they never change afterwards); older windows are closed VOD
playlists. When one ends, the card looks up the next recording and carries on.
They have no live view.

Before 0.7 live was that growing playlist, 20 s behind. On Wi-Fi cameras it
buffered often: SS reports a live recording's end as the last data it wrote,
which lags on Wi-Fi, so the playlist grew in late, uneven steps (a 10 s slot
arrived as 8 + 2 or 3 + 7 s) and the few seconds buffered ran out. Before 0.8
recordings were HLS everywhere, played by hls.js: a seek waited for a 10 s
segment to be downloaded from SS and remuxed by HA (1-3 s), grid followers
were kept in step by seeking into those segments.

## Frigate detections

Optional (the integration's options): with Frigate as the detector and SS as
the recorder, each Frigate **review item** with an object of interest
(person, car, dog, cat by default; alert or detection alike) becomes one SS
bookmark on the same camera, so it is on the card's timeline and in its
event list, and in DS cam / the SS client. Every animal is called "Animal".

Needs, on Frigate's side (verified on 0.18; the review topic is 0.14+):

- **Recording enabled** for the cameras (`record.enabled: true`): Frigate
  makes no review items, and publishes nothing on `<prefix>/reviews`, for a
  camera that doesn't record. SS doing the recording, Frigate's own can be
  small: alerts and detections only, of the detect stream, a few days.
- Frigate on the MQTT broker HA's MQTT integration uses.
- Zones and `review.*.required_zones` decide what becomes a review; this
  integration takes every review with an object of interest.

How it works:

- Read from `<prefix>/reviews` on MQTT (HA's MQTT integration, connected to
  Frigate's broker). Frigate cameras are matched to SS cameras by name,
  ignoring case, spaces and punctuation (`drive_way` = "Drive Way"); where
  the names differ (an SS camera named "前门" or "Porch Cam"), the options'
  second step maps each SS camera to its Frigate camera(s). An unmatched one
  is logged once.
- `new`: bookmark from the review's start, named after its objects
  ("Person, Car", "Animal"); its end is open (30 s, or up to now) until `end`
  sets it. `update`: renamed as objects or zones are added; a review that
  only now has an object of interest (a bicycle, then a person) is bookmarked
  then. The comment ("Frigate alert in porch
  [frigate <review id>]") names the review, so one that ends after a
  restart still finds its bookmark.
- Written with the documented `ThirdParty.Bookmark.Create` / `Edit` (epoch
  times). The DSM account needs no more than playback rights for it.
- Each new bookmark fires **`surveillance_station_detection`** once, with
  `camera`, `camera_id`, `objects` (as named: `["Person", "Animal"]`),
  `labels` (Frigate's: `["person", "dog"]`), `zones`, `severity`, `start`,
  `review_id`, `bookmark_id`, `image` (signed, see below), `thumbnail` (SS's
  frame, 320 px) and `url` (the card at that moment, if a dashboard path is
  set).
- **The image**, with the options' **Frigate URL** set (Frigate's API as HA
  reaches it, e.g. `http://frigate:5000`, the internal port): Frigate's
  snapshot of the review's foremost object (a person before a car before an
  animal, then the surest), box drawn, at its detect resolution, as it is
  when the phone fetches it (needs `snapshots.enabled` in Frigate). The
  object is in it by construction, and the event goes out 3 s after the
  bookmark (time for a better frame than the review's first). If Frigate
  has no snapshot of it, the same URL gives SS's frame instead; if Frigate
  doesn't answer within 5 s, SS's frame too, and Frigate isn't asked again
  for a minute. Without a Frigate URL: SS's frame of
  the moment Frigate picked (`thumb_time`) from the 4K main stream, 1280 px
  wide; the event waits for SS to have recorded it (SS lists recordings
  0-10 s behind; at most 20 s). That frame is also the bookmark's thumbnail
  in the card (kept up to date as Frigate picks a better one); the link
  still starts at the review's beginning.
- **Frigate's times are its detect stream's**: whatever that stream lags
  behind the camera is how late every review, bookmark and SS frame is. On
  Reolink cameras the RTSP sub stream measured a multiple of its I-frame
  interval behind (8-11 s at 4 s, under 1 s at 1 s): **set the sub stream's
  I-frame interval to 1 s** too. To measure, draw the host's clock on a
  pulled frame (`ffmpeg ... -vf "drawtext=text='%{localtime}'"`) and compare
  it with the camera's on-screen time.
- **Quiet period** (options: 5 min, for animals by default; cars can be
  added; **people never**): no event for a review whose kinds are all quiet
  ones seen on the same camera within it, the dog that wandered off and came
  back; it is still bookmarked. A person, or a kind not seen lately, is
  always announced: a second person arriving is exactly what to hear about,
  also when they join the quiet review later.
  Frigate's own `review.*.cutoff_time` decides when an absence splits a
  review in the first place (alerts 40 s, detections 30 s by default). Here
  detections (dogs, cats) are at 120 s; alerts (people, cars) stay at 40 s,
  since a longer one folds a second person arriving within it into the
  first's review, and so into its one notification.
- One event per review, as soon as it has its bookmark, with the objects
  seen by then: a car that a person later gets out of was announced as
  "Car". Normally that is its first message; if that one failed, a later
  one (even its `end`). Which reviews were announced is kept across
  restarts (with the quiet period), so a review going on over a restart is
  announced neither twice nor never. One first heard of more than 2
  minutes after it began (HA was down), or whose message waited more than 2
  minutes for SS, gets its bookmark but no event: the notification would be
  old news. A review seen going on without being news (only bicycles, or a
  quiet dog) is announced when it becomes news, however long it has lasted.
- Anyone who can publish to Frigate's topic on the broker can make
  bookmarks (and pick the moment whose frame is signed into the event); the
  broker is expected to require a login, as Frigate's does.

### Staying up unattended

What keeps detections flowing without anyone reloading anything (each of
these was tested live: MQTT reload, broker restart, HA restarted with the
broker down, NAS unreachable at runtime and during HA's start):

- A message that fails with a transient SS error is tried twice more, 5 s
  apart, taking the review's newer message if one came meanwhile, and
  looking first for a bookmark the failed try may have made (only its
  answer lost). While SS is known to be failing, not: the next reviews
  shouldn't wait behind timeouts.
- What failed because SS was unreachable is kept (one message per review,
  at most 1000, across restarts, for a day), and the oldest is tried again
  every minute; once one goes through, the others are replayed (after
  anything fresh, and back to waiting if SS fails again): an outage loses
  no bookmark (and, past 2 minutes, sends no stale notification). Only a
  bookmark made or found counts as SS being back, not reads that work.
  DSM's "not now" codes (unknown error; API or method not there, as while
  the SS package is stopped or updating; session gone) count as
  unreachable. Any other error code (bad parameters, no permission, SS's
  own 400 and up) is not kept: it would only come again. Reviews not
  handled yet at an unload or an HA restart (written at HA's final write:
  HA doesn't unload entries when it stops), and the one in flight, are
  kept the same way.
- Messages waiting for SS are one per review (a later one replaces the
  earlier: it says everything that one did), at most 1000 reviews.
- The SS camera list is read again every 10 minutes and after any failure:
  a camera replaced in SS under the same name gets a new id.
- HA's MQTT is waited for as long as it takes (it may be starting,
  reloading or retrying its broker), checking every minute.
- Setup with the NAS unreachable (rebooting), or answering oddly: an entry
  that has been set up before (its NAS serial known) starts anyway, and
  the client logs in on first use, so everything works the moment SS
  answers, rather than after HA's setup backoff (up to 10 minutes, during
  which not even Frigate's reviews would be received). A new entry needs
  the serial first: HA retries it. Never a `setup_error` that waits for
  someone; answers that aren't what SS should send are `SSError`s in the
  library.
- Refused logins: a wrong password asks for reauth and is not tried again
  for 30 minutes (DSM's auto-block); DSM blocking the host (407) is not a
  wrong password: no login for a minute, then again.
- A problem lasting 10 minutes becomes an issue in **Settings → Repairs**,
  gone by itself once it clears: MQTT not available, bookmarks failing
  (checked again every minute) or refused by SS (for more than one review,
  until one is made again),
  Frigate offline (its `<prefix>/available`).
- A notification cut short (unload, restart while waiting for the frame)
  isn't counted as sent: the review's next message sends it, within the
  2 minutes. The bridge's state is written on unload, before a reload
  reads it.
- Diagnostics show the bridge: subscribed, Frigate online, and every review
  message's fate (ignored and why, coalesced, dropped, retried, failed,
  bookmarked, announced or not), plus the last error; with a Frigate URL,
  how the notification images went (`images_frigate`, `images_ss`, of those
  `images_no_snapshot`) and `last_image_error`.
- The card retries a stream it gave up on every minute while on screen, so
  a wall display comes back after the NAS reboots.

A notification is an automation on that event. The repository ships a
blueprint for the companion app,
[`blueprints/automation/surveillance_station/detection_notification.yaml`](blueprints/automation/surveillance_station/detection_notification.yaml)
(import it in **Settings → Automations & scenes → Blueprints → Import
blueprint** with that file's URL): a phone, which cameras, objects and
severities, and it sends the title, time, frame and link, one notification
per review. Or by hand, e.g.:

```yaml
triggers:
  - trigger: event
    event_type: surveillance_station_detection
    event_data: {camera: Front Door}
actions:
  - action: notify.mobile_app_phone
    data:
      title: "{{ trigger.event.data.objects | join(', ') }} at {{ trigger.event.data.camera }}"
      message: "{{ trigger.event.data.start | timestamp_custom('%H:%M:%S') }}"
      data:
        image: "{{ trigger.event.data.image }}"
        clickAction: "{{ trigger.event.data.url }}"  # Android
        url: "{{ trigger.event.data.url }}"          # iOS
        tag: "{{ trigger.event.data.review_id }}"
```

The card follows such a link also when it is already on screen (HA navigates
in place, without reloading): `?ss_camera=&ss_time=` selects the camera and
plays from 3 s before the detection. Once followed, the link is taken out of
the address, so going back or closing a dialog doesn't return to it; a
camera it adds to the grid is added for now, not to the grid saved for next
time.

## Resource use

Every buffer has a cap, and the only thing kept on disk is thumbnails:

| Where | What | Bound |
|---|---|---|
| HA memory | remuxed segments (a 10 s segment is 3.5-6.5 MB here) | `SEGMENT_CACHE_BYTES` = 96 MB, LRU |
| HA memory | playback sessions (segment plan + playlist text) | 64 sessions, 4 h idle TTL, 24 h window |
| HA memory | downloads being remuxed | 4 at a time (`MAX_PARALLEL_FETCHES`) |
| HA memory | queued segment fetches (HLS) | cancelled once every client that asked has gone, unless already downloading |
| HA `/tmp` | one scratch file per remux (ffmpeg needs a seekable input) | deleted when the remux ends; `ss_vod_*.mp4` left by a crash are swept at startup |
| HA | live relays | pass-through (a slow viewer slows the read from SS, nothing queues in HA); 16 at most |
| Browser memory | each stream | 8-20 s behind the playhead, trimmed as it goes; ahead, live 0.8 s (a backlog of 90 fragments drops to the next keyframe), recordings at most ~4 s (then SS is paused) |
| HA memory | event thumbnails (JPEG, 320 px, 10-20 KB) | 16 MB LRU (+256 B per entry, so "nothing recorded" answers count too; those expire after 5 min); 2 made at a time, one job per frame however many ask, cancelled once nobody waits for it |
| HA disk | event thumbnails, `<config>/.cache/surveillance_station/thumbnails/` (left out of HA backups) | 64 MB, least recently used removed first; kept across restarts, so a thumbnail is made from the recording once; an entry's are deleted with the entry |
| HA disk | notification images (1280 px, from the 4K main stream), `<config>/.cache/surveillance_station/images/` | 128 MB, the same way |
| HA memory | the bookmark list of each entry | re-read after 15 s; one fetch at a time, whose result (or error, kept 5 s) every waiting request shares |
| Browser disk | thumbnails | `private, max-age=172800, immutable` (a thumbnail of a past moment never changes, and its URL stays the same all day and across HA restarts) |
| Browser disk | segments | none: served `Cache-Control: no-store` (the URLs are per-session, so a cached copy would never be used again) |

On the NAS, every segment download adds a line to the Surveillance Station
log (an hour of a 4-camera grid is ~1400 lines); SS's own log retention
setting bounds it.

## Security model

Streams: `surveillance_station/live` returns a single-use URL (a random
256-bit token, valid 30 s) for one camera, live or from a time; HA opens SS's
stream with its own session and relays it, so the SS session id never reaches
the browser. Of what the browser sends, only `time=<epoch seconds>`,
`pause=true|false` and `speed=<0.5|1|2|4|8|16>` reach SS (matched whole);
everything else just shows the viewer is still there. At most 16 streams at
once; unloading the entry closes them.

The HLS playlist and segment URLs carry a random 256-bit token and need no HA
auth header, because a `<video>` cannot add one. The token is issued only to an
authenticated WebSocket client (`surveillance_station/vod`). It is scoped to one
camera and one time window, and expires 4 hours after its last use (at most
64 sessions, least recently used evicted first). DSM credentials stay in the
config entry. Login is a POST, and errors are rebuilt without request URLs, so
neither the password nor the SS session id reaches logs or browsers.

Event thumbnail URLs (`/api/surveillance_station/thumbnail/…`) work the same
way, since an `<img>` can't send a header either: the WebSocket hands out URLs
carrying an expiry (the end of the next UTC day) and an HMAC-SHA256 of the
path and expiry, under a key made once and kept in HA's storage
(`surveillance_station.thumbnail_key`), so the URLs survive restarts. Each opens one JPEG of one camera at one moment.
Anything unsigned, tampered with or expired is a **404**. HA's own signed
paths (`async_sign_path`) were used at first and dropped: HA answers a stale
one (after every HA restart, or after a day) with 401, and counts every 401 as
a failed login, so a wall tablet left open would get its IP banned under
`login_attempts_threshold`.

Frigate's images (a notification's, a search result's) are signed the same
way, under `/api/surveillance_station/frigate_image/…`; HA fetches them from
Frigate and passes on only a JPEG or WebP (checked by its bytes). Only ids
shaped like Frigate's (`1790406867.462609-6jc58g`) go into Frigate's URLs.

Every HA user can use the card and so see every camera, like HA's own camera
entities; there is no per-user camera permission.

## Surveillance Station API notes (verified on SS 9.x, DSM 7)

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
  in the future (or playing into the present) gives the real-time stream.
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

## Known limitations

- A bookmark in the hour repeated when DST ends is placed in the first of the
  two (SS lists bookmark times as local times without an offset).
- The timeline re-lists every shown camera's recordings for the whole span
  once a minute while it follows the present; at 7 d that is a few `Event.List`
  pages per camera. Watching live on a span of 1 h or less it does so every 5 s
  (~40 ms per camera), so a recording that ends stops growing on the timeline;
  its bar still overshoots by 15-20 s before drawing back.
- A card that must fit a short screen (a phone in landscape, 4 cameras) gets
  small cells: the chips, controls and timeline need about 230 px. Fullscreen
  is the way to watch there.
- The event list's once-a-minute refresh reads the newest page. A bookmark
  created now for a moment older than the newest 30 shows up when the list
  starts over (after 12 h, a camera change, or a reload).
- After HA restarts, HA rebuilds the dashboard, so the card starts over (live)
  instead of resuming a paused moment.
- A hole inside a recording file (the camera dropped out, the file went on)
  is sent by SS as that much time with nothing in it. The card notices after
  1.5 s of silence and asks for 16x until frames come again, so a hole costs
  about 1.5 s of "Buffering" whatever its length.
- A jump lands on the keyframe before the time asked for (SS starts there),
  so up to one GOP early; buffered frames are then played from there.
  Frames (thumbnails, notification images) are cut the same way: SS's
  Download takes whole seconds and starts at the keyframe at or before
  them, and the file it returns has no finer time to correct by. **Set the
  cameras' I-frame interval to 1 s** (e.g. 15 at 15 fps): with a 4 s GOP a
  notification's frame can be 4 s before the moment Frigate picked, the
  person not in it yet.
- Each camera in a grid is a stream from the NAS to the browser (via HA) at
  full recording quality: a 4-camera grid of 4K H.265 needs a device that
  decodes four of them at once, and at 4-8x a multiple of that.

- Scale: a grid opens on the first four cameras unless the card names
  them (`cameras:`); each cell is its camera's full-quality stream from the
  NAS through HA, and HA relays at most 16 at once (all viewers together).
  Mind the viewing device's decoders and HA's network with more.
- The card lists all bookmarks every 15 s while open (SS's time filter for
  them doesn't work): with tens of thousands of bookmarks that gets slow.
- The card's layout choices (cameras, grid, span) are remembered per
  browser, shared by all cards in it.
- One Frigate instance per NAS (one MQTT topic prefix per entry).
- Not tried: CMS recording servers (cameras on another NAS), Archive Vault
  and camera-edge (SD card) recordings, disabled cameras (not shown), two
  cameras with the same name.
- The card's text is English; its times follow the HA profile's 12/24 h
  setting. Grid cells are 16:9.
- A notification's image link stays valid for one to two days. Without a
  Frigate URL (or when Frigate gives none and the image falls back to SS's
  frame), with SS recording on motion, a recording starting more than ~8 s
  after Frigate's frame leaves the notification without an image.
- Sound is the camera's own codec, played if the browser can (AAC: all;
  Opus / MP3: most; G.711 / G.726, many cameras' default: none, in MSE); the
  Sound button says when it can't. HLS playback (browsers without MSE)
  leaves out audio MP4 can't carry.

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

## Tests

```
scripts/test.sh            # all tests, in a Python 3.14 container
scripts/test.sh -k reauth  # extra pytest arguments
```

`.github/workflows/ci.yml` runs the same suite on push to `main` and on pull
request (plain Python 3.14 via `actions/setup-python`, not the local
container - a GitHub-hosted runner doesn't need the memory/time cap
`scripts/test.sh` uses to protect the dev host), plus `hassfest` and the HACS
integration check. Coverage must clear 95% separately for
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
  script's header; `npm install` in `tests/browser` first). Covered: live; jumps (buffered,
  over the socket, from live); a recording-file boundary; 1/2/4/8x; pause
  (SS paused by flow control) and resume; the 4-camera grid in step after
  jumps, speed changes, pause and a master change; a follower's 72-minute gap
  and a gap the master lands in; gaps all cameras share; a 29 s hole inside a
  recording file (bridged in ~1.5 s); sound kept within ~0.1 s of the video;
  the no-MSE path (native HLS) on a phone-sized viewport; the live timeline
  scrolling with the present.
