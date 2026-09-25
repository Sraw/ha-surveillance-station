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
# Parallel Download+remux jobs against the NAS.
MAX_PARALLEL_FETCHES = 4

THUMBNAIL_URL = "/api/surveillance_station/thumbnail"
# Event thumbnails (~10-20 KB each) kept in memory, by size.
THUMBNAIL_CACHE_BYTES = 16 * 1024 * 1024
# What a cached thumbnail costs besides its bytes, so misses (b"") count too.
THUMBNAIL_ENTRY_BYTES = 256
# "Nothing recorded then" is re-checked after this long.
THUMBNAIL_MISS_SECONDS = 300
# Parallel thumbnail jobs: fewer than playback's, which must not wait on them.
MAX_PARALLEL_THUMBNAILS = 2
# How long a signed thumbnail URL handed to the card stays valid.
THUMBNAIL_URL_TTL_HOURS = 24
# Largest page of the event list.
BOOKMARK_PAGE_MAX = 100
# SS returns all bookmarks in one list; the card's timeline, event list and
# its scrolling all read one cached copy, this fresh.
BOOKMARK_CACHE_SECONDS = 15
# A failed bookmark fetch is answered from memory this long.
BOOKMARK_ERROR_SECONDS = 5
# Longest time range the timeline may ask for (the card's widest span is 7 d).
MAX_QUERY_WINDOW_SECONDS = 8 * 86400
