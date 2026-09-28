[← README](../README.md)

# Known limitations

- **Time-lapse and the GPU**: SIGKILLing `hevc_qsv` decodes of SS's
  4512x2512 time-lapse while other QSV sessions run hung an Iris Xe iGPU
  (i915 `GPU HANG ... in hevc_qsv`, reproduced: 4 hangs in 36 transcodes
  with random kills, none in 35 kills of a lone one or in 33 parallel
  transcodes left to finish). The reset also stalled Frigate's QSV decoders
  on the same GPU until Frigate was restarted. Hence one transcode at a
  time; a hardware one, once running, finishes whoever leaves (only waiting
  for its turn is cancelled), HA's shutdown waits for it, and a 30 s timeout
  stops it with SIGTERM before SIGKILL.
- **Broken time-lapse frames hang the GPU**: a Reolink E1 Outdoor Pro on
  firmware v3.1.0.5714 (Wi-Fi) now and then leaves a frame in its 4K
  time-lapse with one of its two slices (one per tile column) missing, or
  with the second one from another picture (a P slice in an all-intra
  stream). Software decoders conceal it. The Iris Xe decodes these frames
  with both VDBoxes, one tile column each, and hangs on the first kind;
  ffmpeg crashes on the second. This happened on every such cut, with
  Intel's media driver 25.2.6 (Frigate's) and 26.2.1, and with ffmpeg 7.0
  and 8.1. The same camera model on older firmware never did. So before a
  GPU transcode of an H.265 cut, the integration reads the NAL unit headers
  of each frame (under a millisecond). If any frame isn't whole, it
  re-muxes the cut without that frame (`noise=drop`), and the frame before
  stays on screen for a 30th of a second. The re-muxed cut must hold
  exactly the frames that were kept, or it doesn't reach the GPU. After
  this change, 166 consecutive segments of that camera ran without a hang.
- Time-lapse throughput is bounded by the NAS: a second of daytime 4K
  time-lapse is ~30 MB, the gigabit link carries ~110 MB/s from a finished
  file, and SS reads the file it is still writing at only ~50 MB/s. Today's
  time-lapse can therefore stall in daylight, and faster playback is not
  offered.

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
