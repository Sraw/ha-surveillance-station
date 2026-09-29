"""Constants for the Surveillance Station playback integration."""

DOMAIN = "surveillance_station"

CONF_VERIFY_SSL = "verify_ssl"
# DSM's HTTPS port (5000 is its HTTP one).
DEFAULT_PORT = 5001

# Where the bundled Lovelace card is served from.
STATIC_URL = "/surveillance_station_static"
CARD_FILENAME = "ss-timeline-card.js"

VOD_URL = "/api/surveillance_station/vod"
# A playback session (playlist + its segment URLs) stays valid this long.
VOD_SESSION_TTL_SECONDS = 4 * 3600
VOD_MAX_SESSIONS = 64
# Longest window one playlist may cover (the card asks for much less).
VOD_MAX_WINDOW_SECONDS = 24 * 3600
# A window whose end is at least this close to now becomes a live session.
LIVE_THRESHOLD_SECONDS = 60
# Remuxed segments kept in memory, by size: a 10 s segment is 3.5-6.5 MB for
# these 4K H.265 streams, so this holds ~20 of them (a few per camera in a grid).
SEGMENT_CACHE_BYTES = 96 * 1024 * 1024
# Parallel Download+remux jobs against the NAS.
MAX_PARALLEL_FETCHES = 4
# Parallel time-lapse jobs: each pulls ~30 MB per second of daytime video
# from the NAS (the gigabit link is the limit) and holds the cut in memory;
# their transcodes run one at a time (the GPU is Frigate's too).
MAX_PARALLEL_TRANSCODES = 3
# How video is transcoded (today: time-lapse; playback and live are only
# re-muxed, and a single frame is always decoded on the CPU): "auto" on an
# Intel GPU (QSV) where there is a usable one, else on the CPU (H.264);
# "gpu" never on the CPU (no time-lapse without a GPU); "cpu" never on the GPU.
CONF_TRANSCODER = "transcoder"
TRANSCODERS = ["auto", "gpu", "cpu"]
DEFAULT_TRANSCODER = "auto"
# The list of time-lapse files is fetched again after this long (a file
# grows by a frame every 8 s; new files start once a day).
TIMELAPSE_LIST_SECONDS = 60

LIVE_URL = "/api/surveillance_station/live"
# A live-stream token must be used (the socket opened) within this long.
LIVE_TOKEN_TTL_SECONDS = 30
# Unused live-stream tokens kept at most (the oldest go first): a few for
# every stream HA may relay.
LIVE_TOKENS_MAX = 64
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
# A camera with no recording in progress and none ended this long before a
# moment isn't being recorded then (motion-only recording): not waited for.
# Longer than SS's gap between two files of a continuous recording.
RECORDING_GAP_SECONDS = 60
# ...but only after this long: an SS recording on motion may start (or be
# listed) a few seconds after Frigate saw the object.
NOT_RECORDING_GRACE_SECONDS = 8
# How often thumbnail_when_recorded asks SS whether the moment is written yet.
THUMBNAIL_POLL_SECONDS = 2
# "Nothing recorded then" is re-checked after this long (a moment less than
# RECORDING_GAP_SECONDS ago: after THUMBNAIL_RECENT_MISS_SECONDS).
THUMBNAIL_RECENT_MISS_SECONDS = 5
THUMBNAIL_MISS_SECONDS = 300
# Parallel thumbnail jobs: fewer than playback's, which must not wait on them.
MAX_PARALLEL_THUMBNAILS = 2
# A signed thumbnail URL handed to the card stays valid this long past the
# end of the current (UTC) day, so it's the same URL all day.
THUMBNAIL_URL_TTL_HOURS = 24
# Largest page of the event list.
BOOKMARK_PAGE_MAX = 100
# SS returns all bookmarks in one list; the card's timeline, event list and
# its scrolling all read one cached copy, this fresh. Bookmarks HA makes or
# changes itself are listed afresh at once.
BOOKMARK_CACHE_SECONDS = 60
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
CONF_FRIGATE_URL = "frigate_url"  # Frigate's HTTP API, for its snapshots as notification images
# {SS camera name: Frigate camera name(s), comma-separated}, for names that don't match.
CONF_FRIGATE_CAMERAS = "frigate_cameras"
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
# With Frigate's snapshot as the image (its API configured), the event waits
# this long instead: Frigate keeps each object's best frame so far, and the
# review's first moment (the object just coming into view, a cat taken for a
# person before the person arrives) is rarely it.
FRIGATE_SNAPSHOT_SETTLE_SECONDS = 3
# Objects whose snapshot a notification image tries, foremost first.
FRIGATE_SNAPSHOT_TRIES = 3
# A Frigate bookmark's thumbnail (event list, search results): its snapshot this tall.
FRIGATE_THUMB_HEIGHT = 180
# Thumbnails built from Frigate at once (an event list page asks for 30).
FRIGATE_THUMB_PARALLEL = 4
# Those of reviews that are over, kept in memory, by size (~10-20 KB each).
FRIGATE_THUMB_CACHE_BYTES = 8 * 1024 * 1024
# Frigate's API answers a request within this.
FRIGATE_API_TIMEOUT = 10
# A notification's image: Frigate's snapshot within this (all its requests
# together), else SS's frame within the rest; a phone waits ~30 s at most.
FRIGATE_IMAGE_BUDGET_SECONDS = 5
FRIGATE_IMAGE_FALLBACK_SECONDS = 18
# After Frigate failed to give an image, SS's frame straight away for this long.
FRIGATE_IMAGE_BACKOFF_SECONDS = 60
FRIGATE_IMAGE_URL = "/api/surveillance_station/frigate_image"
# Smart search (Frigate's semantic search): results asked of Frigate, and
# at most this many shown (one per review).
FRIGATE_SEARCH_ASK = 100
FRIGATE_SEARCH_MAX = 50
# The whole search (Frigate's answers and the SS camera list) within this.
FRIGATE_SEARCH_TIMEOUT_SECONDS = 15
# Frigate (0.18) may answer a search that ran into another client's with
# nothing: an empty answer is asked once more, this much later.
FRIGATE_SEARCH_EMPTY_RETRY_SECONDS = 0.5
# A Frigate object is a bookmark's when their times overlap, give or take
# this (a review, and so its bookmark, starts when an object qualifies,
# which is after the object itself was first seen).
FRIGATE_BOOKMARK_SLACK = 2
# A bookmark's objects by time: looked for back this far (one can have
# started long before its review: a car parked for hours, then moving).
FRIGATE_BOOKMARK_LOOKBACK = 3600
# Kinds offered to filter the event list by, at most.
KIND_CHIPS_MAX = 8
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
FRIGATE_CAMERAS_RETRY = 60  # a listing that failed is not asked for again sooner, by a review or a search
FRIGATE_DECIDED_MAX = 512  # reviews remembered (across restarts) as announced or not
FRIGATE_DEFERRED_MAX_AGE = 86400  # a review SS didn't take for a day is given up on
# Health: a problem that lasts this long becomes a Repairs issue (cleared on
# recovery); checked every FRIGATE_HEALTH_INTERVAL.
FRIGATE_ISSUE_AFTER_SECONDS = 600
FRIGATE_HEALTH_INTERVAL = 60
# Surveillance Station unreachable this long: a Repairs issue (its text in
# strings.json says 10 minutes).
SS_ISSUE_AFTER_SECONDS = 600
CONF_FRIGATE_QUIET = "frigate_quiet_minutes"
CONF_FRIGATE_QUIET_KINDS = "frigate_quiet_kinds"
# A camera's review with only these kinds, all seen there within this many
# minutes, fires no event: the dog that wandered off and came back. Never
# people: a second person arriving is exactly what to be told about.
DEFAULT_FRIGATE_QUIET_MINUTES = 5
FRIGATE_QUIET_KINDS = ["Car", "Animal"]
DEFAULT_FRIGATE_QUIET_KINDS = ["Animal"]
# Muting notifications: the kinds that get a switch, and the id of the mute
# buttons on a notification (the blueprint builds them, the integration
# answers them: SS_MUTE:<entry id>:<seconds>:<camera key, empty for all>).
MUTE_KINDS = ["Person", "Car", "Animal"]
MUTE_ACTION_PREFIX = "SS_MUTE"
MUTE_ACTION_MAX_SECONDS = 7 * 86400
# Bookmarks whose thumbnail is another moment than their start (the frame a
# detector picked, where the object shows best), remembered across restarts.
BOOKMARK_FRAMES_MAX = 20_000
