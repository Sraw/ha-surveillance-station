[← README](../README.md)

# The timeline card

`custom:ss-timeline-card` is registered by the integration as a Lovelace
resource when the first entry is set up: add it to any dashboard, nothing to
install separately.

## Options

```yaml
type: custom:ss-timeline-card
cameras: [Drive Way, Front Door]   # optional: the grid's cameras (default: all)
camera: Drive Way     # optional: the one to start on
view: single          # optional: start on one camera rather than the grid
span: 3600            # optional: timeline width in seconds (15m .. 7d)
clock: false          # optional: hide the date/time on the video
live_badge: false     # optional: hide the LIVE badge on the video
camera_names: false   # optional: hide the camera names in grid cells
entry_id: ...         # optional: which NAS, with more than one entry
```

The eye button in the controls hides or shows each of those three; the
viewer's choice is remembered in the browser and wins over the options.

## Using it

- **Opens playing**: live (about 1 s behind real time, marked LIVE), or the
  moment a link asked for. Coming back to the view resumes live if it was live.
- **Fits the screen**: from the camera chips to the timeline, the card sizes
  the video so it all fits in the window (cells stay 16:9; the grid gets
  narrower when full width would be too tall). Narrow cards (phones) get one
  compact row of controls: the ±30 s buttons and the time labels are dropped.

- **Grid or one camera**: the square / grid button in the controls switches
  between the two. In the grid (2 side by side, 3-4 in 2x2, more in 3
  columns) the camera chips add or remove a camera (tinted = shown, a ring
  in the camera's colour = the leader; the last one stays). With one camera
  the chips switch which one, and the grid keeps its cameras for when it
  comes back. Double-tapping a grid cell shows that camera alone. Each camera
  has a colour, used for its chip, its cell label, its timeline pins and its
  event rows. The mode, the grid's cameras and the camera watched are
  remembered per browser (`localStorage`) and win over `view` / `cameras` /
  `camera`.
- **Leader**: tap a cell. It has the sound and the clock, and the others
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
- **Keyboard**: the timeline is a slider over its span. The arrow keys move the
  playhead 1 % of the span, Page Up / Down 10 %, Home / End to its ends; the
  cameras go there once the keys stop (the right end, when it is now, is
  live). Each pin is a button (Tab to it, Enter opens its event), and every
  icon button has a name for screen readers.
- **Clock**: the overlay button toggles it; remembered per browser.
- **Fullscreen**: the stage (video + a slim auto-hiding control bar) goes
  fullscreen and asks for landscape (`screen.orientation.lock`; honoured on
  Android / the companion app, ignored where the browser doesn't allow it).
  On an iPhone, which has no element fullscreen, the leader's own video player
  goes fullscreen instead.
- **Zoom**: pinch or double-tap (single view) zooms up to 8x, drag pans,
  double-tap resets; the mouse wheel zooms in fullscreen. At 1x vertical
  swipes still scroll the page (`touch-action: pan-y`).

## Events

The **Events** list is a collapsible sidebar on wide cards (≥ 1000 px) and
sits under the timeline on narrow ones. It holds every SS bookmark of the
cameras shown, newest first, grouped by day, each with a thumbnail of the
moment (a Frigate bookmark's, with a Frigate URL set: Frigate's snapshot of
its foremost object, box drawn, 180 px tall, as the notification's image;
once Frigate has deleted the review, the snapshot of the object it still has
from that camera, time and kind; with none, or Frigate not answering in 5 s,
SS's frame, and Frigate isn't asked for thumbnails for a minute after it
failed; 4 are made at a time). Frigate deletes a review with its recordings
(`record.alerts/detections.retain.days`): keep those as long as
`snapshots.retain` (and SS's recordings), so a thumbnail is always found
through its review, the same object as at first; by time, with two of a kind
there, it may be the other one. It loads 30 at a time as you scroll (cursor-paged, so events created
meanwhile don't shift it). Once a minute it is brought up to date: new events
are merged in by time (an event can be bookmarked after a later one), events
deleted in SS go away, and after more than a page of new ones, or 12 h, it
starts over. Rows are kept across refreshes, so thumbnails aren't reloaded.
Tapping one
makes that camera the leader and plays from 3 s before it; events under the
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
  seen in it of the bookmark's kinds, image to image). Frigate finds; SS
  plays: each result is placed on the SS camera its Frigate camera maps to,
  at its start time, and only one on a Frigate bookmark of its kind on that
  camera then (the one it began in, else the one it overlaps most) is a
  result: that bookmark, listed once. What Frigate saw and let go (outside
  the zones that count, a kind not bookmarked) isn't an event, so it isn't
  found either. Matched by camera and time, not by Frigate's review ids:
  Frigate deletes reviews with its own recordings (days), but keeps tracked
  objects as long as their snapshots. Results come best first, each with its
  bookmark's thumbnail (as in the event list), and the kind
  chips filter them too (by the bookmark's name, before the first 30 are
  taken). Tapping one plays SS's recording from 3 s before the object
  appears in its bookmark (one there since before the bookmark plays from
  the bookmark's start). Frigate ranks the best 100 of all cameras before
  filtering by camera, so with a few cameras shown there may be fewer
  results. Needs `semantic_search.enabled` in Frigate; on a machine whose
  iGPU also decodes and detects, keep it on the CPU (`model_size: small`):
  with `large` on the iGPU, a burst of searches here hung the GPU, and the
  cameras' decoders came back crippled until Frigate restarted. Frigate
  (0.18) answers two semantic searches at once with nothing, so this
  integration asks them one at a time (and an empty answer once more).
  Frigate deletes a tracked object, and so what can be found, with its
  snapshot (`snapshots.retain`): keep that as long as SS keeps recordings.

## Links to a moment

URL parameters override on load: `?ss_camera=<name|id>&ss_time=<epoch seconds>`.
A notification can link straight to a moment this way.

## How playback works

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
- **Grid**: every camera has its own stream; the others follow the leader's
  wall-clock time. SS never sends faster than asked, so a follower that is
  behind can't catch up by playing faster: it asks for 1 s (times the speed)
  past the leader's time and holds its first frame until the leader gets
  there (landing behind anyway, it asks again with twice the lead). A few
  tenths off are made up by ±20 % speed; a camera with nothing recorded holds
  its next footage behind a "No recording" veil until the leader reaches it.
  Measured on a 4-camera grid: in step to ~0.15 s, 1-2 s after a jump.
- **Sound**, when unmuted, comes from the leader only (live, or recordings at
  1x), through its own `<audio>` kept on the video's wall-clock time (a video
  that waited for audio would stall on every burst).

Browsers without MSE (Safari before 17.1) play recordings as HLS instead: a
window that reaches the recent past is an HLS EVENT playlist that grows as SS
records (segments are only published once they end on the 10 s grid behind
real time, so they never change afterwards); older windows are closed VOD
playlists. When one ends, the card looks up the next recording and carries on.
They have no live view.
