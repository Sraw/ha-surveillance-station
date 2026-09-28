[← README](../README.md)

# Time-lapse

`custom:ss-timelapse-card` plays a camera's **Surveillance Station
time-lapse** (a task set up in SS; this only reads it) one day at a time:
camera chips, a row of days, the video with the time it shows, and a bar
with what the day has (click or drag to go there; the playhead goes there at
once, however long that part takes to load). The bar zooms to 24h / 6h / 1h
/ 15m of the day (remembered in the browser), follows the playhead when
zoomed in and pans with its arrows (it stays put for 15 s after a pan). The
skip buttons move 10 or 30 s of the time-lapse - their tooltip says how much
real time that is (40 min / 2 h at 240x); ±30 s is hidden on narrow screens. Options:
`camera` (name or id to start on) and `entry_id`. There is no speed control:
time-lapse is already fast (240x by default).

A time-lapse task writes files of up to 6 minutes of video, each rolling over
when full (about a day at 240x, from whenever the task started, not at
midnight; SS's slowed-down stretches around its own events fill a file
sooner). A day is the parts of the files that fall between the NAS's
midnights, found by mapping wall time linearly across each file as SS's own
player does; its boundaries are whole seconds of video (4 minutes at 240x).

SS stores time-lapse as all-intra H.265 at the camera's full resolution
(4512x2512 here: 90-240 Mbps), too much for a browser, so HA **transcodes**
it to 1280 wide: H.265 where the browser plays it (~2 Mbps), else H.264.
Segments are 4 s of video, cut on the NAS (`recEvtType=3`), streamed into
memory, transcoded and served as HLS fMP4 under the same session tokens as
recordings; the card fetches them itself into MSE (the playlist goes to
`<video>` where there is no MSE). A day opens in ~3 s and a seek to
something not yet fetched plays in ~2 s: the segment there is fetched alone
(the NAS link is the limit, see below), then three at a time ahead.

**Only one time-lapse plays at a time**, whoever watches: opening one ends
the previous session (its URLs answer 410, and its card offers to take it
back rather than reopening by itself).

What transcodes follows the option **Video transcoding** (it is for
everything the integration transcodes; today that is the time-lapse -
playback and live are only re-muxed, and a single frame for a thumbnail
is always decoded on the CPU):

- **Automatic** (default): an **Intel GPU (QSV)** when the container has
  a usable one (checked once, logged, and in diagnostics as
  `timelapse_hardware`), the CPU otherwise.
- **GPU only**: never the CPU; without a GPU there is no time-lapse.
- **CPU only**: never the GPU.

A GPU check that times out (the GPU busy) is not taken for "no GPU": that
time-lapse fails (`gpu_busy`) and the next one checks again, rather than
loading the CPU. On the CPU it is H.264, on half the cores (decoding,
scaling and encoding each), and every ffmpeg that fetches, re-muxes or
transcodes video runs niced (10) below Home Assistant. On a 12-thread i5-1235U, 4 s of 4512x2512
time-lapse takes 3.0 s on 6 threads (2.6 s on 12): it keeps up, with
little to spare, and a seek takes several seconds. The
official HA image ships ffmpeg with QSV but no GPU driver: this repo's owner
runs it with `intel-media-driver` + `onevpl-intel-gpu` added and `/dev/dri`
passed in. Transcodes run one at a time, and a hardware one is never
stopped by its viewer leaving (see [Known limitations](limitations.md)).
