# Surveillance Station Playback for Home Assistant

Watch your **Synology Surveillance Station** cameras, live and recorded, from a
Home Assistant dashboard. Surveillance Station keeps doing the recording; this
integration plays it back in HA on a scrubbable timeline, and can turn
**Frigate** detections into SS bookmarks and phone notifications that open the
recording at that moment.

![The timeline card: live video, the timeline with its bookmarks, the events list](https://raw.githubusercontent.com/Sraw/ha-surveillance-station/main/docs/images/live.png)

## Assumed setup

The docs assume a common setup: each camera offers a **main stream** at high
resolution (4K H.265 here), which SS records, and a **sub stream** at a lower
resolution, which Frigate watches. The numbers you'll find here (bitrates,
sizes, decoder and GPU load) are for a 4K main stream; with a lower
resolution they are smaller, but the reasoning is the same.

## Why this exists: SS stores, Frigate detects

The idea is a clean split of jobs. **Surveillance Station is the recorder**: it
keeps every camera's original stream on the NAS, for as long as you set.
**Frigate is the detector**: it looks at the cameras and says what happened.
This integration joins them, so a Frigate detection becomes a bookmark on SS's
own recording, and you watch it all in one place in HA.

Why that is better than letting SS detect, or letting Frigate record:

- **More accurate detection.** Frigate runs real object-detection models (on a
  Coral, GPU or OpenVINO) with per-camera zones, masks, score thresholds and
  labels, so "a person at the front door" is not "something moved".
- **More flexible.** Detection is tuned per camera, and the detector, the model
  and Frigate itself can be changed or upgraded whenever you like, without
  touching a single recording. Frigate's smart search ("white car", "similar to
  this one") comes with it.
- **SS just stores.** Nothing here asks SS to do what it is worse at, and
  Frigate does not have to be your archive: it can watch a cheap low-resolution
  stream while SS keeps the full-quality one.
- **Recording never depends on detection.** If Frigate, MQTT or HA is down or
  being rebuilt, SS keeps recording. Detections that were missed are put back
  as bookmarks when everything is up again.
- **Full quality, one archive.** Playback is SS's original H.265/H.264, never
  transcoded. Bookmarks are in SS, so DS cam and the SS client see them too,
  not only HA.
- **Detections you can act on.** Each one is an HA event, and the ready-made
  blueprint turns it into a phone notification with Frigate's snapshot that
  opens the recording at that moment.

You can also use the card without Frigate, as a live and recorded viewer for SS.

## Screenshots

<p align="center">
  <img src="https://raw.githubusercontent.com/Sraw/ha-surveillance-station/main/docs/images/grid.png" alt="The camera grid, all cameras in step" width="100%">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/Sraw/ha-surveillance-station/main/docs/images/phone.png" alt="The compact phone layout" width="48%">
  <img src="https://raw.githubusercontent.com/Sraw/ha-surveillance-station/main/docs/images/mute.png" alt="The mute card" width="48%">
</p>

<sub>The pictures of the cameras in these screenshots are drawn, not recorded.</sub>

## Features

- **Live and recorded video on one timeline**: live is about 1 s behind real
  time; tap or drag anywhere on a 15-minute to 7-day timeline to play the
  recording from there, at 1-8x. Video is SS's original H.265/H.264, never
  transcoded, so it is full quality and costs HA almost no CPU.
- **Camera grid in step**: watch several cameras side by side, all following
  the same moment; one camera has the sound, jumps and speed apply to all.
- **Snapshots**: one button saves the picture on screen — a camera's frame,
  or the whole grid — as a JPEG, live or in playback.
- **Events**: SS bookmarks are pins on the timeline and a list with
  thumbnails, filterable by kind (Person, Car, Animal, …). Tap one to play it.
- **Frigate detections** (optional): each Frigate review becomes an SS
  bookmark, visible in HA, DS cam and the SS client, and fires an event for
  notifications. A ready-made blueprint sends a phone notification with
  Frigate's snapshot that opens the recording at that moment, and alerts again
  when a person joins what started as a cat.
- **Smart search** (with Frigate): find events in words ("white car") or by
  "similar to this one", then play them from SS's recording.
- **Time-lapse**: plays SS's day-by-day time-lapse, transcoded for the
  browser (Intel GPU if available).
- **Unattended**: survives NAS reboots, MQTT and broker restarts without
  losing a bookmark; lasting problems show up in *Settings → Repairs*.
- **Works in the HA apps**: phones get a compact layout, fullscreen,
  pinch-zoom; notification links open the card at the moment.

## Requirements

- Surveillance Station 9 on DSM 7 (tested: SS 9.3, DSM 7.2). Setup checks
  that the NAS has the Web APIs used and says so if not.
- Home Assistant 2026.9 or newer.
- NAS, HA and the viewing devices on NTP: the card lines up SS's frame times
  with the device's clock.
- A browser that decodes **H.265** if your cameras record H.265: the HA
  Android/iOS apps, Safari, and Chrome/Edge with hardware decoding do;
  **Firefox does not**.
- Recommended: cameras with a **1-second I-frame interval** (GOP = frame
  rate). Jumps and notification images land on a keyframe, so a longer
  interval makes them up to that much early.

## Installation

### HACS

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Sraw&repository=ha-surveillance-station&category=integration)

Or in HACS → ⋮ → *Custom repositories*, add
`https://github.com/Sraw/ha-surveillance-station` as an **Integration**. Then
download *Surveillance Station Playback* and restart Home Assistant.

### Manual

Copy `custom_components/surveillance_station` into your HA config's
`custom_components/` and restart Home Assistant. HA installs the
[`synology-ss-playback`](https://pypi.org/project/synology-ss-playback/)
library by itself.

## Setup

1. **In DSM**, create a dedicated user for HA with Surveillance Station
   access only: a viewer-level SS privilege profile that can play back and
   download recordings. It must not use two-step verification (exempt it
   from a policy that enforces 2FA).
2. **In HA**, *Settings → Devices & services → Add integration →
   Surveillance Station Playback*: the NAS host, port (HTTPS on 5001, the
   default; 5000 for http) and that user.
3. **Add the card** to a dashboard:

   ```yaml
   type: custom:ss-timeline-card
   ```

   It is registered automatically. See [the card's options](docs/timeline-card.md#options).

### Frigate notifications (optional)

Needs Frigate on the MQTT broker HA uses, with recording enabled for its
cameras.

1. In the integration's **Configure**, turn on *Bookmark Frigate detections*.
   Set the *Frigate URL* (e.g. `http://frigate:5000`) for Frigate's snapshots
   in notifications and smart search, and the *dashboard path* of the card so
   notifications link to it. Cameras are matched by name; the next step maps
   those that differ.
2. Import the notification blueprint:

   [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FSraw%2Fha-surveillance-station%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fsurveillance_station%2Fdetection_notification.yaml)

   and create an automation from it: choose the phone, cameras and objects.

### Time-lapse (optional)

Set up a time-lapse task in Surveillance Station, then add
`type: custom:ss-timelapse-card` to a dashboard.

Unlike recordings, which are played as SS stored them, a time-lapse is
transcoded by HA. SS writes it as one all-intra 4K frame every 8 s of real
time (at the default 240x), ~80-130 Mbps on average once played at 30 fps
(roughly 20-30 times a camera's own stream), too much to send to a browser,
so HA turns it into a ~2 Mbps 1280-wide stream, on an Intel GPU if it has
one. [Why, and the numbers](docs/timelapse.md#why-it-is-transcoded).

## Documentation

- [The timeline card](docs/timeline-card.md): options, controls, events,
  smart search, how playback works
- [Frigate detections and notifications](docs/frigate.md): what gets
  bookmarked, the `surveillance_station_detection` event, quiet periods,
  writing your own automation
- [Time-lapse](docs/timelapse.md): the card, GPU/CPU transcoding
- [Known limitations](docs/limitations.md)
- [Security model](docs/security.md) and [resource use](docs/resource-use.md)
- [Development](docs/development.md): layout, tests, releasing, SS API notes

## Removing

Take the card off your dashboards, delete the entry under *Settings →
Devices & services* (the last one also removes the card's resource), then
remove the integration in HACS (or delete the folder) and restart HA.

## License

[Apache-2.0](LICENSE)
