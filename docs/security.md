[← README](../README.md)

# Security model

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

A notification's (and a bookmark's) Frigate image is signed the same way, under
`/api/surveillance_station/frigate_image/…`; HA fetches it from Frigate
and passes on only a JPEG or WebP (checked by its bytes). Only ids
shaped like Frigate's (`1790406867.462609-6jc58g`) go into Frigate's URLs.

Every HA user can use the card and so see every camera, like HA's own camera
entities; there is no per-user camera permission.
