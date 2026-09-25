"""Constants for the Surveillance Station playback integration."""

DOMAIN = "surveillance_station"

CONF_VERIFY_SSL = "verify_ssl"
DEFAULT_PORT = 5000

# Where the bundled Lovelace card is served from.
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

LIVE_URL = "/api/surveillance_station/live"
# A live-stream token must be used (the socket opened) within this long.
LIVE_TOKEN_TTL_SECONDS = 30
# A live relay whose browser sent nothing (the card keeps alive every 10 s)
# for this long is closed.
LIVE_IDLE_SECONDS = 90
# How often the relay pings Surveillance Station to keep its socket open.
LIVE_KEEP_ALIVE_SECONDS = 10
# Live streams relayed at once (each is one camera's ~4-5 Mbps).
MAX_LIVE_STREAMS = 16

THUMBNAIL_URL = "/api/surveillance_station/thumbnail"
# Event thumbnails (~10-20 KB each) kept in memory, by size.
THUMBNAIL_CACHE_BYTES = 16 * 1024 * 1024
# ...and on disk (under HA's cache directory), so they outlive a restart.
THUMBNAIL_DISK_BYTES = 64 * 1024 * 1024
# Widths: the event list's thumbnails, and the larger image for notifications
# (from the 4K main stream: Frigate's own snapshots are its 640x360 detect stream).
THUMBNAIL_WIDTH = 320
LARGE_IMAGE_WIDTH = 1280
LARGE_IMAGE_DISK_BYTES = 128 * 1024 * 1024
# What a cached thumbnail costs besides its bytes, so misses (b"") count too.
THUMBNAIL_ENTRY_BYTES = 256
# "Nothing recorded then" is re-checked after this long.
THUMBNAIL_MISS_SECONDS = 300
# Parallel thumbnail jobs: fewer than playback's, which must not wait on them.
MAX_PARALLEL_THUMBNAILS = 2
# A signed thumbnail URL handed to the card stays valid this long past the
# end of the current (UTC) day, so it's the same URL all day.
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
# SS moves a recording-in-progress's end forward about every 10 s; one whose
# end is older than this has stopped, whatever its flag still says. (NAS and
# HA clocks agree to within a second, both on NTP.)
LIVE_END_STALE_SECONDS = 15

# Frigate detections as bookmarks (options).
CONF_FRIGATE = "frigate"
CONF_FRIGATE_TOPIC = "frigate_topic"
CONF_FRIGATE_OBJECTS = "frigate_objects"  # Frigate labels bookmarked, whatever the review's severity
CONF_FRIGATE_LINK = "frigate_link"  # dashboard path of the card, for the event's url
DEFAULT_FRIGATE_TOPIC = "frigate"
DEFAULT_FRIGATE_OBJECTS = ["person", "car", "dog", "cat"]
# Named "Animal" in bookmarks and events, one kind for all of them.
FRIGATE_ANIMALS = frozenset(
    {"bear", "bird", "cat", "cow", "deer", "dog", "fox", "goat", "horse", "kangaroo", "rabbit", "raccoon",
     "sheep", "skunk", "squirrel", "zebra", "elephant", "giraffe"}
)
DETECTION_EVENT = f"{DOMAIN}_detection"
# A review still going on gets a bookmark this long (or up to now) until its end arrives.
FRIGATE_OPEN_BOOKMARK_SECONDS = 30
# The event waits at most this long for SS to have recorded the moment (its frame).
FRIGATE_EVENT_WAIT_SECONDS = 20
FRIGATE_TRACKED_MAX = 256  # reviews in progress remembered
FRIGATE_QUEUE_MAX = 1000  # reviews waiting for SS; the oldest are dropped beyond
# A review bookmarked later than this after it began (SS was unreachable, HA
# restarted mid-review) fires no event: a notification would be old news.
FRIGATE_ANNOUNCE_MAX_AGE = 120
# A review message that failed with a transient SS error is tried this many
# more times, this far apart (not while SS is known to be failing: then the
# queue would stall behind timeouts).
FRIGATE_RETRIES = 2
FRIGATE_RETRY_SECONDS = 5
FRIGATE_MQTT_RETRY_SECONDS = 60  # waiting for HA's MQTT to come up
FRIGATE_CAMERAS_TTL = 600  # SS camera list refreshed at least this often
FRIGATE_DECIDED_MAX = 512  # reviews remembered (across restarts) as announced or not
FRIGATE_DEFERRED_MAX_AGE = 86400  # a review SS didn't take for a day is given up on
# Health: a problem that lasts this long becomes a Repairs issue (cleared on
# recovery); checked every FRIGATE_HEALTH_INTERVAL.
FRIGATE_ISSUE_AFTER_SECONDS = 600
FRIGATE_HEALTH_INTERVAL = 60
CONF_FRIGATE_QUIET = "frigate_quiet_minutes"
CONF_FRIGATE_QUIET_KINDS = "frigate_quiet_kinds"
# A camera's review with only these kinds, all seen there within this many
# minutes, fires no event: the dog that wandered off and came back. Never
# people: a second person arriving is exactly what to be told about.
DEFAULT_FRIGATE_QUIET_MINUTES = 5
FRIGATE_QUIET_KINDS = ["Car", "Animal"]
DEFAULT_FRIGATE_QUIET_KINDS = ["Animal"]
# Bookmarks whose thumbnail is another moment than their start (the frame a
# detector picked, where the object shows best), remembered across restarts.
BOOKMARK_FRAMES_MAX = 20_000
