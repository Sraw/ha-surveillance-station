"""Constants for the Surveillance Station playback integration."""

DOMAIN = "surveillance_station"

CONF_VERIFY_SSL = "verify_ssl"
DEFAULT_PORT = 5000

# Where the bundled Lovelace card and hls.js are served from.
STATIC_URL = "/surveillance_station_static"
CARD_FILENAME = "ss-timeline-card.js"

VOD_URL = "/api/surveillance_station/vod"
# A playback session (playlist + its segment URLs) stays valid this long.
VOD_SESSION_TTL_SECONDS = 4 * 3600
VOD_MAX_SESSIONS = 64
# Longest window one playlist may cover (the card asks for much less).
VOD_MAX_WINDOW_SECONDS = 24 * 3600
# Remuxed segments kept in memory, by size: a 10 s segment is 3.5-6.5 MB for
# these 4K H.265 streams, so this holds ~20 of them (a few per camera in a grid).
SEGMENT_CACHE_BYTES = 96 * 1024 * 1024
# Scratch files ffmpeg reads from (SS puts the moov box at the end).
TEMP_PREFIX = "ss_vod_"
REMUX_TIMEOUT_SECONDS = 30
# Parallel Download+remux jobs against the NAS.
MAX_PARALLEL_FETCHES = 4
