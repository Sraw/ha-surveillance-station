[← README](../README.md)

# Resource use

Every buffer has a cap, and the only thing kept on disk is thumbnails:

| Where | What | Bound |
|---|---|---|
| HA memory | remuxed segments (a 10 s segment is 3.5-6.5 MB here) | `SEGMENT_CACHE_BYTES` = 96 MB, LRU |
| HA memory | playback sessions (segment plan + playlist text) | 64 sessions, 4 h idle TTL, 24 h window |
| HA memory | downloads being remuxed | 4 at a time (`MAX_PARALLEL_FETCHES`) |
| HA memory | time-lapse cuts being transcoded (4 s of 4K all-intra: 50-200 MB each, in an anonymous in-memory file, never on disk) | 3 at a time (`MAX_PARALLEL_TRANSCODES`), 1 of them on the GPU; one time-lapse session at a time |
| HA memory | queued segment fetches (HLS) | cancelled once every client that asked has gone, unless already downloading |
| HA `/tmp` | one scratch file per remux (ffmpeg needs a seekable input) | deleted when the remux ends; `ss_vod_*.mp4` left by a crash are swept at startup |
| HA | live relays | pass-through (a slow viewer slows the read from SS, nothing queues in HA); 16 at most |
| Browser memory | each stream | 8-20 s behind the playhead, trimmed as it goes; ahead, live 0.8 s (a backlog of 90 fragments drops to the next keyframe), recordings at most ~4 s (then SS is paused) |
| HA memory | event thumbnails (JPEG, 320 px, 10-20 KB) | 16 MB LRU (+256 B per entry, so "nothing recorded" answers count too; those expire after 5 min); 2 made at a time, one job per frame however many ask, cancelled once nobody waits for it |
| HA disk | event thumbnails, `<config>/.cache/surveillance_station/thumbnails/` (left out of HA backups) | 64 MB, least recently used removed first; kept across restarts, so a thumbnail is made from the recording once; an entry's are deleted with the entry |
| HA disk | notification images (1280 px, from the 4K main stream), `<config>/.cache/surveillance_station/images/` | 128 MB, the same way |
| HA memory | the bookmark list of each entry | re-read after 15 s; one fetch at a time, whose result (or error, kept 5 s) every waiting request shares |
| Browser disk | thumbnails | `private, max-age=172800, immutable` (a thumbnail of a past moment never changes, and its URL stays the same all day and across HA restarts); a Frigate bookmark's snapshot `private, max-age=3600` (Frigate may pick a better one while the review goes on) |
| Browser disk | segments | none: served `Cache-Control: no-store` (the URLs are per-session, so a cached copy would never be used again) |

On the NAS, every segment download adds a line to the Surveillance Station
log (an hour of a 4-camera grid is ~1400 lines); SS's own log retention
setting bounds it.
