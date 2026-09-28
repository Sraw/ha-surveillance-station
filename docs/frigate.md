[← README](../README.md)

# Frigate detections and notifications

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
  ("Person, Car", "Animal"); its end is open (30 s, or up to now; for a
  message that waited out an SS outage, up to when the review's latest
  message came) until `end` sets it. `update`: renamed as objects are
  added; a review that only now has an object of interest (a bicycle, then
  a person) is bookmarked then.
  A bookmark deleted in SS while its review goes on is made again by the
  review's next message.
  The comment ("Frigate alert [frigate <review id>]") names the review, so
  one that ends after a restart still finds its bookmark. Zones are left
  out of it (only some cameras have them; they are in the event).
  The tag must end the comment: text added after it (in DS cam or SS)
  makes it a bookmark like any other, for its review, smart search and
  thumbnails alike.
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
  has no snapshot of it, the next object's; of none, the same URL gives
  SS's frame instead; if Frigate doesn't answer within 5 s (or fails), SS's
  frame too, and Frigate isn't asked again for a minute. Without a Frigate URL: SS's frame of
  the moment Frigate picked (`thumb_time`) from the 4K main stream, 1280 px
  wide; the event waits for SS to have recorded it (SS lists recordings
  0-10 s behind; at most 20 s). That frame is also the bookmark's thumbnail
  in the card without a Frigate URL (kept up to date as Frigate picks a
  better one); the link
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
  seen by then. Normally that is its first message; if that one failed, a
  later one (even its `end`). **Again if something more important joins
  the review later** (person > car > anything else > animals): a person
  after a dog, or getting out of a car, is a second event for the same
  review — the blueprint's notification uses the review id as its `tag`,
  so it replaces the first one ("Animal" becomes "Person, Animal", with
  the person's snapshot) and the phone alerts again. A dog after a person,
  or a second animal, is not; nor a kind that may be quiet (a car, if cars
  are made quiet, joining a dog's review). An object seen while the first
  event waits for its frame is simply in that event. Which reviews were
  announced is kept across restarts (with the quiet period), so a review
  going on over a restart is announced neither twice nor never
  (downgrading below 0.18 forgets it: the stored format changed). One
  first heard of more than 2 minutes after it began (HA was down), or
  whose message waited more than 2 minutes for SS, gets its bookmark but
  no event: the notification would be old news. A review seen going on
  without being news (only bicycles, or a quiet dog) is announced when it
  becomes news, however long it has lasted.
- Anyone who can publish to Frigate's topic on the broker can make
  bookmarks (and pick the moment whose frame is signed into the event); the
  broker is expected to require a login, as Frigate's does.

## Staying up unattended

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
  gone by itself once it clears: MQTT not available (at start, or its
  broker lost later), bookmarks failing
  (checked again every minute) or refused by SS (for more than one review,
  until one is made again),
  Frigate offline (its `<prefix>/available`).
- A notification cut short (unload, restart while waiting for the frame)
  isn't counted as sent: the review's next message sends it, within the
  2 minutes. The bridge's state is written on unload, before a reload
  reads it.
- Diagnostics show the bridge: subscribed, Frigate online, and every review
  message's fate (ignored and why, coalesced, dropped, retried, failed,
  bookmarked, announced or not; `announced_again`: the further
  notifications of reviews something more important joined), plus the last error; with a Frigate URL,
  how the notification images went: `images_frigate` (Frigate's snapshot),
  `images_ss` (SS's frame instead), `images_failed` (neither: the phone got
  no image), `images_no_snapshot` (Frigate had none for that review),
  `last_image_error` (Frigate's) and `last_ss_image_error` (SS's frame
  failing too). A review or object Frigate no longer has (404), or a
  snapshot that isn't an image, falls to the next object (3 at most) or
  SS's frame; anything else (unreachable, too slow, refused: a wrong URL
  or port, failing) pauses asking Frigate for a minute and shows in
  `last_image_error`.
- The card retries a stream it gave up on every minute while on screen, so
  a wall display comes back after the NAS reboots.

A notification is an automation on that event. The repository ships a
blueprint for the companion app,
[`blueprints/automation/surveillance_station/detection_notification.yaml`](../blueprints/automation/surveillance_station/detection_notification.yaml)
(import it in **Settings → Automations & scenes → Blueprints → Import
blueprint** with that file's URL): a phone, which cameras, objects and
severities, and it sends the title, time, frame and link, one notification
per review (updated in place when something more important joins it). Or by hand, e.g.:

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
        ttl: 0            # Android: deliver now; at normal priority an idle
        priority: high    # phone (Doze) held one for 8 minutes
```

The card follows such a link also when it is already on screen (HA navigates
in place, without reloading): `?ss_camera=&ss_time=` selects the camera and
plays from 3 s before the detection. Once followed, the link is taken out of
the address, so going back or closing a dialog doesn't return to it; a
camera it adds to the grid is added for now, not to the grid saved for next
time.
