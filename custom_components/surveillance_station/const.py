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
# Remuxed segments kept in memory (~6.5 MB each for a 4K H.265 stream).
SEGMENT_CACHE_SIZE = 12
# Parallel Download+remux jobs against the NAS.
MAX_PARALLEL_FETCHES = 4
