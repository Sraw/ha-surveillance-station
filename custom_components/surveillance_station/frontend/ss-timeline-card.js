/*
 * ss-timeline-card: play back Synology Surveillance Station recordings with a
 * scrubbable wall-clock timeline and the SS bookmarks laid on top of it.
 *
 * Talks to the surveillance_station integration over the HA WebSocket:
 *   surveillance_station/cameras | recordings | bookmarks | bookmark_page | search |
 *   live | vod | vod_runs
 * Video is SS's own stream (`live`: a single-use WebSocket URL, live or the
 * recordings from a time on), played through MSE by StreamFeed; each frame
 * carries its wall-clock time. Browsers without MSE (Safari before 17.1) get
 * HLS instead: `vod` returns a playlist URL plus `runs`, the map between
 * playlist time and wall-clock time (a new run starts after every gap).
 *
 * One Player per camera shown; one camera shown is the single view, several
 * are the grid. One player is the master: it has the sound and the clock and
 * the others follow its wall-clock time (see Player.follow). The timeline and
 * the event list cover the cameras shown.
 *
 * Layout: camera chips, the stage (sized so chips-to-timeline fit on the
 * screen), controls and timeline; the event list is a sidebar on wide cards
 * and sits below on narrow ones. The card opens playing: live, or the moment
 * a link asked for.
 *
 * Card options (all optional):
 *   cameras:  names or ids in the grid (default: all); the viewer's last choice
 *             (the camera chips) wins
 *   camera:   name or id of the camera to start on (the master)
 *   view:     "single" to start on one camera instead of the grid; the
 *             viewer's last choice wins
 *   span:     timeline width in seconds (default 3600); the viewer's last choice wins
 *   clock:    false hides the time on the video; the viewer's last choice wins
 *   live_badge:   false hides the LIVE badge on the video; likewise
 *   camera_names: false hides the camera names in grid cells; likewise
 *   entry_id: which Surveillance Station entry, if there is more than one
 * URL parameters override on load: ?ss_camera=<name|id>&ss_time=<epoch seconds>
 */

const CARD_TAG = "ss-timeline-card";
const CARD_VERSION = "0.15.2";
// After giving up on a stream, it is tried again this often while visible.
const STREAM_RETRY_MS = 60000;
// Cameras a grid opens on when the card names none: each is a full-quality
// stream from the NAS through HA to the browser (more are a chip away).
const DEFAULT_GRID_MAX = 4;

const SPANS = [
  [900, "15m"],
  [3600, "1h"],
  [6 * 3600, "6h"],
  [24 * 3600, "24h"],
  [72 * 3600, "3d"],
  [7 * 24 * 3600, "7d"],
];
const SPEEDS = [1, 2, 4, 8];
const PRE_ROLL = 30; // seconds before the target included in a new window
const WINDOW_AHEAD = 3600; // seconds after the target included in a new window
const LIVE_LAG = 20; // "live" plays this far behind now (segments close at now-5)
const REFRESH_MS = 60_000;
// A recording in progress (live: HA drops the flag once SS stops moving its
// end forward, which it does every ~10 s) is drawn up to now, but never more
// than LIVE_GROW_MAX past the end SS last reported. Following live on a short
// span, recordings are refetched every LIVE_RECS_MS: a bar that ended stops
// growing, and draws back to where it really ended, 15-20 s later; one that
// starts shows up as quickly. On longer spans the overshoot (up to a minute
// until the next refresh) is a few pixels at most.
const LIVE_GROW_MAX = REFRESH_MS / 1000 + 15;
const LIVE_RECS_MS = 5_000;
const LIVE_RECS_SPAN = 3600;
const FOLLOW_PAUSE_MS = 15_000; // after a manual pan, don't snap the view back
// A transparent poster: without one, Android WebView (the HA app) paints its
// default poster, a big grey play arrow, over a video with no frame yet, so
// it flashed on every jump to a bookmark or new window.
const BLANK_POSTER = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
const THUMB_RETRY_MS = 30_000;
const EVENT_PAGE = 30; // events per page of the list; more load as it scrolls
// A Frigate bookmark's comment names its review, as the integration writes (and reads) it.
const FRIGATE_REF = /\[frigate [A-Za-z0-9][A-Za-z0-9._-]{0,63}\]/;
// The list (and its thumbnail links, valid 24-48 h) is reloaded after this long.
const EVENT_LIST_MAX_AGE_MS = 12 * 3600 * 1000;
const TICK_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
// One colour per camera (chip, cell label, timeline pins, event rows); these
// read on both light and dark backgrounds.
const CAM_COLORS = ["#4f8ff7", "#f5a623", "#2dbd8f", "#e5534b", "#a371f7", "#e05aa8", "#1fb5c6", "#c9a227"];
const MIN_STAGE_HEIGHT = 160; // px; below this the page scrolls instead
// What's laid over the video and can be hidden: [pref key and card option, label].
const SHOWS = [
  ["clock", "Time"],
  ["live_badge", "Live badge"],
  ["camera_names", "Camera names"],
];
// Grid sync: how often followers correct, and how.
const SYNC_MS = 500;
// HLS followers, seconds off:
const DRIFT_JUMP = 2; // seek
const DRIFT_NUDGE = 0.1; // speed up / slow down (at most ±20%)
const ZOOM_MAX = 8;
const FS_IDLE_MS = 3000; // fullscreen controls hide after this

const nowS = () => Date.now() / 1000;
// The link parameters out of the address, without a navigation.
const consumeLink = () => {
  const params = new URLSearchParams(location.search);
  if (!params.has("ss_time") && !params.has("ss_camera")) return;
  params.delete("ss_time");
  params.delete("ss_camera");
  const q = params.toString();
  history.replaceState(history.state, "", location.pathname + (q ? "?" + q : "") + location.hash);
};
const pad = (n) => String(n).padStart(2, "0");
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
// Asking for "now" (the Live button, opening the card, a tap past the
// recorded edge) plays the real-time stream. Any earlier time is a recording
// (recordings reach up to about LIVE_LAG behind now), so an event from a few
// seconds ago still plays the event.
const isLiveTime = (t) => t >= nowS() - 2;
// Past the newest recording: that means live.
const liveIfRecent = (t) => (t >= nowS() - LIVE_LAG ? nowS() : t);
// MSE for the real-time stream: iOS Safari (17.1+) only has ManagedMediaSource.
// Without either, live falls back to the newest recordings (HLS, ~20 s behind).
const MSE = window.MediaSource ?? window.ManagedMediaSource;
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const errText = (e) => e?.message ?? e?.code ?? String(e);
/** Merge [start, end] intervals into their sorted union. */
const unionOf = (spans) => {
  const out = [];
  for (const [s, e] of [...spans].sort((a, b) => a[0] - b[0])) {
    const last = out[out.length - 1];
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else out.push([s, e]);
  }
  return out;
};

// Per-viewer preferences. Storage can be unavailable (private mode, WebView
// settings); the card then just uses its config.
const prefs = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(`ss-timeline-card.${key}`);
      return v == null ? fallback : JSON.parse(v);
    } catch (e) {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`ss-timeline-card.${key}`, JSON.stringify(value));
    } catch (e) {
      /* not persisted */
    }
  },
};

// 12 or 24 h, as the user's HA profile says (hass.locale.time_format:
// "12", "24", "language" or "system").
let hour12 = false;
let hour12Key;
function setTimeFormat(locale) {
  const f = locale?.time_format;
  const key = `${f}|${locale?.language}`;
  if (key === hour12Key) return; // hass is set on every state change
  hour12Key = key;
  if (f === "12" || f === "24") hour12 = f === "12";
  else {
    const lang = f === "system" ? undefined : locale?.language;
    hour12 = Boolean(new Intl.DateTimeFormat(lang, { hour: "numeric" }).resolvedOptions().hour12);
  }
}
function fmtTime(t, seconds = true) {
  const d = new Date(t * 1000);
  if (hour12) {
    const h = d.getHours() % 12 || 12;
    return `${h}:${pad(d.getMinutes())}${seconds ? ":" + pad(d.getSeconds()) : ""} ${d.getHours() < 12 ? "AM" : "PM"}`;
  }
  return isoTime(d, seconds);
}
function isoTime(d, seconds = true) {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? ":" + pad(d.getSeconds()) : ""}`;
}
function fmtDate(t) {
  return new Date(t * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}
function fmtDay(t) {
  return new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
}
/**
 * Tick times in [start, end], every `step` seconds on the local clock: hour
 * and day steps are counted in local time, so they stay on whole hours and
 * midnights across a DST change (a day is then 23 or 25 hours).
 */
function ticksOf(start, end, step) {
  const out = [];
  if (step < 3600) {
    // Sub-hour steps divide an hour, and zone offsets are whole quarter hours.
    const tzo = new Date(start * 1000).getTimezoneOffset() * 60;
    for (let t = Math.ceil((start - tzo) / step) * step + tzo; t <= end; t += step) out.push(t);
    return out;
  }
  const d = new Date(start * 1000);
  d.setMinutes(0, 0, 0);
  const hours = step / 3600;
  if (step >= 86400) d.setHours(0);
  else d.setHours(Math.floor(d.getHours() / hours) * hours);
  const next = () => (step >= 86400 ? d.setDate(d.getDate() + step / 86400) : d.setHours(d.getHours() + hours));
  while (d.getTime() / 1000 < start) next();
  for (; d.getTime() / 1000 <= end; next()) out.push(d.getTime() / 1000);
  return out;
}
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60 ? ` ${s % 60}s` : ""}`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
function toLocalInput(t) {
  const d = new Date(t * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${isoTime(d)}`;
}
function hevcSupport() {
  const MS = window.ManagedMediaSource || window.MediaSource;
  if (!MS) return null;
  return ["hvc1.1.6.L153.B0", "hvc1.1.6.L150.90", "hvc1.1.6.L120.90"].some((c) =>
    MS.isTypeSupported(`video/mp4; codecs="${c}"`)
  );
}

const VEIL_HTML = `
  <div class="veil off">
    <div class="spin"></div>
    <ha-icon></ha-icon>
    <div class="vtext"></div>
    <div class="vsub"></div>
    <button data-act="retry">Retry</button>
  </div>`;

/**
 * Show a veil: kind "loading" (spinner), "empty" (nothing to show here) or
 * "error" (with Retry). Empty text hides it.
 */
function setVeil(veil, text, kind = "loading", sub = "") {
  veil.classList.toggle("off", !text);
  if (!text) return;
  veil.classList.remove("loading", "empty", "error");
  veil.classList.add(kind);
  veil.querySelector("ha-icon").setAttribute("icon", kind === "error" ? "mdi:alert-circle-outline" : "mdi:video-off-outline");
  veil.querySelector(".vtext").textContent = kind === "loading" ? `${text}…` : text;
  veil.querySelector(".vsub").textContent = sub;
}

const STYLE = `
  :host { display: block; }
  ha-card { overflow: hidden; container-type: inline-size; }
  [hidden] { display: none !important; }
  ha-icon { --mdc-icon-size: 20px; }

  /* Buttons take only theme colours: the text colour is never the background colour. */
  button { font: inherit; font-size: 14px; color: var(--primary-text-color); background: transparent;
    border: 1px solid var(--divider-color); border-radius: 18px; padding: 0 12px; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 34px;
    white-space: nowrap; -webkit-tap-highlight-color: transparent; }
  button:hover { background: color-mix(in srgb, var(--primary-text-color) 8%, transparent); }
  button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  button.icon { border: none; border-radius: 50%; width: 36px; min-width: 36px; height: 36px; padding: 0; }
  button.on { background: color-mix(in srgb, var(--primary-color) 22%, transparent);
    border-color: var(--primary-color); color: var(--primary-text-color); }
  button.icon.on { color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 16%, transparent); }

  /* Layout: main column, and the event list beside it (wide) or under it (narrow). */
  .layout { display: flex; flex-direction: column; }
  .main { min-width: 0; }
  @container (min-width: 1000px) {
    .layout { display: grid; grid-template-columns: minmax(0, 1fr) 340px; }
    .layout.noside { grid-template-columns: minmax(0, 1fr); }
    /* Not stretched to a taller list: _fit measures what the column holds. */
    .main { align-self: start; }
    /* The list is as tall as the main column, or down to the bottom of the
       screen where that is lower (--room, see _fit), and scrolls inside it. */
    /* (.layout: these must outrank the narrow layout's rules further down,
       which a container query alone doesn't; it capped the list at 60vh.) */
    .layout .side { height: 0; min-height: max(100%, var(--room, 0px)); border-left: 1px solid var(--divider-color); border-top: none; }
    .layout .ev-list { max-height: none; }
  }
  .layout.noside .side { display: none; }

  /* Camera chips: one row, scrolling sideways if it has to. */
  .head { display: flex; align-items: center; gap: 8px; padding: 8px 8px 6px 12px; }
  .cams { display: flex; gap: 6px; flex: 1; min-width: 0; overflow-x: auto; scrollbar-width: none; }
  .cams::-webkit-scrollbar { display: none; }
  /* Chips cut off at the edge fade out: there are more to scroll to. */
  .cams { -webkit-mask-image: linear-gradient(to right, #000 calc(100% - 28px), transparent);
    mask-image: linear-gradient(to right, #000 calc(100% - 28px), transparent); padding-right: 22px; }
  .cams button { font-size: 13px; padding: 0 12px 0 10px; min-height: 32px; color: var(--secondary-text-color); }
  .cams .dot { width: 10px; height: 10px; border-radius: 50%; border: 2px solid var(--cam); box-sizing: border-box; }
  .cams button.shown { color: var(--primary-text-color); border-color: color-mix(in srgb, var(--cam) 55%, var(--divider-color));
    background: color-mix(in srgb, var(--cam) 14%, transparent); }
  .cams button.shown .dot { background: var(--cam); }
  /* The master (sound + clock), when several are shown. */
  .cams.multi button.master { border-color: var(--cam); box-shadow: inset 0 0 0 1px var(--cam); font-weight: 500; }
  .head .evtoggle { flex: none; }
  .badge { font-size: 11px; min-width: 18px; padding: 0 5px; border-radius: 9px; line-height: 18px;
    background: var(--secondary-background-color); color: var(--secondary-text-color); }

  /* Stage: one 16:9 cell per camera shown; sized by the card to fit the screen. */
  .stagebox { display: flex; justify-content: center; }
  .stage { position: relative; background: #000; user-select: none; -webkit-user-select: none; width: 100%; }
  /* No camera yet (or the list failed): room for the message. */
  .stage.nocam { aspect-ratio: 16 / 9; max-height: 50vh; }
  .stage.grid { display: grid; grid-template-columns: repeat(var(--cols, 2), 1fr); gap: 2px; }
  .cell { position: relative; overflow: hidden; background: #000; touch-action: pan-y; }
  .cell.zoomed { touch-action: none; }
  .vp { position: relative; transform-origin: center center; will-change: transform; }
  video::-webkit-media-controls-overlay-play-button, video::-webkit-media-controls-start-playback-button { display: none !important; }
  video { display: block; width: 100%; aspect-ratio: 16 / 9; background: #000; object-fit: contain; }
  .stage.grid .cell.master { outline: 2px solid var(--cam, var(--primary-color)); outline-offset: -2px; z-index: 1; }
  .label { position: absolute; left: 6px; bottom: 6px; z-index: 2; padding: 1px 6px; border-radius: 4px;
    font-size: 11px; color: #fff; background: rgba(0,0,0,.55); pointer-events: none; display: flex; align-items: center; gap: 5px; }
  .label::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--cam); }
  .stage.single .label { display: none; }
  .clock { position: absolute; top: 6px; left: 6px; z-index: 3; padding: 1px 6px; border-radius: 4px;
    background: rgba(0,0,0,.45); color: #fff; font-variant-numeric: tabular-nums; font-size: 12px; pointer-events: none; }
  .stage.noclock .clock, .stage.nolive .livetag, .stage.nonames .label { display: none; }
  .livetag { position: absolute; top: 6px; right: 6px; z-index: 3; padding: 1px 7px; border-radius: 4px; font-size: 11px;
    font-weight: 600; letter-spacing: .04em; color: #fff; background: #d93025; pointer-events: none; }

  /* Fullscreen: the stage fills the screen; a control bar hides when idle. */
  .stage:fullscreen { width: 100vw !important; height: 100vh; display: flex; align-items: center; justify-content: center; }
  .stage.grid:fullscreen { display: grid; grid-template-rows: repeat(var(--rows, 2), 1fr); align-items: stretch; }
  .stage:fullscreen .cell { width: 100%; height: 100%; display: flex; align-items: center; }
  .stage:fullscreen .vp { width: 100%; height: 100%; }
  .stage:fullscreen video { height: 100%; aspect-ratio: auto; }
  .fsbar { display: none; position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%); z-index: 4;
    gap: 4px; align-items: center; padding: 4px 8px; border-radius: 24px; background: rgba(0,0,0,.6);
    transition: opacity .3s; }
  .stage:fullscreen .fsbar { display: flex; }
  .stage.idle .fsbar { opacity: 0; pointer-events: none; }
  .fsbar button { color: #fff; }
  .fsbar button:hover { background: rgba(255,255,255,.15); }
  .fsbar .fsclock { color: #fff; font-size: 13px; font-variant-numeric: tabular-nums; padding: 0 6px; }

  /* Loading / error veil: the current frame (or a still of the last one)
     blurred behind a spinner or icon, a line of text and a sub-line. */
  .still { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; background: #000;
    opacity: 0; transition: opacity .2s; pointer-events: none; }
  .still.show { opacity: 1; }
  .veil { position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 10px; padding: 16px; text-align: center; color: #fff;
    background: rgba(0,0,0,.3); backdrop-filter: blur(18px) saturate(1.15); -webkit-backdrop-filter: blur(18px) saturate(1.15);
    opacity: 1; visibility: visible; transition: opacity .25s, visibility 0s; }
  .veil.off { opacity: 0; visibility: hidden; pointer-events: none; transition: opacity .25s, visibility 0s .25s; }
  .spin { width: 44px; height: 44px; border-radius: 50%; border: 3px solid rgba(255,255,255,.25);
    border-top-color: #fff; animation: ss-spin .9s linear infinite; }
  @keyframes ss-spin { to { transform: rotate(360deg); } }
  .veil ha-icon { --mdc-icon-size: 42px; opacity: .9; }
  .veil.error ha-icon { color: #ff8a80; }
  .vtext { font-size: 16px; font-weight: 500; text-shadow: 0 1px 4px rgba(0,0,0,.6); max-width: 90%; }
  .vsub { font-size: 13px; opacity: .85; font-variant-numeric: tabular-nums; text-shadow: 0 1px 3px rgba(0,0,0,.6); }
  .vsub:empty { display: none; }
  .veil button { color: #fff; border-color: rgba(255,255,255,.5); }
  .veil:not(.loading) .spin, .veil.loading ha-icon, .veil:not(.error) button { display: none; }
  .stage.grid .veil { gap: 6px; padding: 8px; }
  .stage.grid .spin { width: 28px; height: 28px; }
  .stage.grid .veil ha-icon { --mdc-icon-size: 28px; }
  .stage.grid .vtext { font-size: 13px; }
  /* In a grid cell the label names the camera and the clock has the time. */
  .stage.grid .vsub { display: none; }

  .warn { padding: 6px 12px; font-size: 13px; color: var(--warning-color, #b58100); }
  .warn:empty { display: none; }

  /* Controls: one row. */
  .controls { position: relative; display: flex; align-items: center; gap: 2px; padding: 4px 8px; }
  .controls .live { margin-left: 4px; font-size: 13px; padding: 0 10px; min-height: 30px; }
  .controls .live.on { border-color: #d93025; background: color-mix(in srgb, #d93025 18%, transparent); }
  .controls .live ha-icon { --mdc-icon-size: 18px; }
  .spacer { flex: 1; }
  select, input { font: inherit; font-size: 13px; color: var(--primary-text-color); background: transparent;
    border: 1px solid var(--divider-color); border-radius: 8px; padding: 0 4px; min-height: 30px; color-scheme: light dark; }
  select option { color: var(--primary-text-color); background: var(--ha-card-background, var(--card-background-color, var(--primary-background-color))); }
  /* The date popover hangs off the controls row (not its button), so it can
     never be wider than the card: on a phone the input shrinks to fit. */
  .jump { position: absolute; right: 8px; bottom: calc(100% + 6px); z-index: 6; display: flex; gap: 6px; padding: 8px;
    max-width: calc(100% - 16px); box-sizing: border-box;
    border-radius: 12px; background: var(--ha-card-background, var(--card-background-color, var(--primary-background-color)));
    border: 1px solid var(--divider-color); box-shadow: 0 4px 16px rgba(0,0,0,.25); }
  .jump .when { flex: 1 1 auto; min-width: 0; }
  .jump button { flex: none; }
  .jump.shows { flex-direction: column; gap: 2px; padding: 6px; }
  .shows button { justify-content: flex-start; border: none; border-radius: 8px; padding: 0 10px 0 6px; }
  .shows button ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); }
  .shows button.on ha-icon { color: var(--primary-color); }
  .shows button.on { background: transparent; }
  @container (max-width: 520px) {
    .controls .wide { display: none; }
    .controls .live .txt { display: none; }
    .controls .live { padding: 0 8px; }
    .range .long { display: none; }
    .spans button { padding: 0 7px; }
    .clock { font-size: 11px; }
    button.icon { width: 34px; min-width: 34px; height: 34px; }
  }

  /* Timeline */
  .tlbar { display: flex; align-items: center; gap: 4px; padding: 0 8px; }
  .range { flex: 1; min-width: 0; font-size: 12px; color: var(--secondary-text-color); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; padding-left: 4px; }
  .range .short { display: none; }
  @container (max-width: 520px) { .range .short { display: inline; } }
  .spans { display: flex; border: 1px solid var(--divider-color); border-radius: 16px; overflow: hidden; flex: none; }
  .spans button { border: none; border-radius: 0; min-height: 28px; padding: 0 9px; font-size: 12px; color: var(--secondary-text-color); }
  .spans button + button { border-left: 1px solid var(--divider-color); }
  .spans button.on { color: var(--primary-text-color); font-weight: 600; }
  .tlbar button.icon { width: 30px; min-width: 30px; height: 30px; }
  .track { position: relative; height: 52px; margin: 4px 12px 10px; touch-action: none; cursor: pointer;
    background: var(--secondary-background-color); border-radius: 6px; user-select: none; }
  .bars { position: absolute; inset: 0; overflow: hidden; border-radius: 6px; }
  .rec { position: absolute; top: 20px; height: 12px; background: color-mix(in srgb, var(--primary-color) 45%, transparent); }
  @supports not (background: color-mix(in srgb, red 50%, blue)) { .rec { background: var(--primary-color); opacity: .45; } }
  .tick { position: absolute; bottom: 0; height: 6px; border-left: 1px solid var(--divider-color); }
  .tick span { position: absolute; bottom: 6px; left: 3px; font-size: 10px; color: var(--secondary-text-color); white-space: nowrap; }
  .nowm { position: absolute; top: 0; bottom: 0; border-left: 2px dashed var(--error-color, #db4437); }
  /* Bookmarks: a pin per event in its camera's colour, with a finger-sized hit area. */
  .bm { position: absolute; top: 3px; height: 14px; min-width: 4px; border-radius: 3px; background: var(--cam);
    box-shadow: 0 0 0 1px var(--secondary-background-color); z-index: 1; }
  .bm::before { content: ""; position: absolute; left: -9px; right: -9px; top: -3px; bottom: -12px; }
  /* Pins closer than a finger: exact hit areas, so taps between them still seek. */
  .track.dense .bm::before { left: -1px; right: -1px; bottom: 0; }
  .bm:hover { filter: brightness(1.2); }
  .ph { position: absolute; top: -4px; bottom: -4px; width: 2px; margin-left: -1px; background: var(--primary-text-color); pointer-events: none; z-index: 2; }
  .ph::before { content: ""; position: absolute; top: 0; left: -5px; border: 6px solid transparent; border-top-color: var(--primary-text-color); }
  .hover { position: absolute; top: -26px; transform: translateX(-50%); padding: 1px 6px; border-radius: 4px; z-index: 3;
    background: var(--primary-text-color); color: var(--card-background-color, #fff); font-size: 12px; pointer-events: none; white-space: nowrap; }

  /* Event list: bookmarks of the cameras shown, newest first, loaded as you scroll. */
  .side { display: flex; flex-direction: column; min-width: 0; }
  .ev-head { display: flex; align-items: center; gap: 8px; padding: 10px 8px 6px 12px; }
  .ev-title { font-weight: 500; }
  /* Under the video (narrow cards, and browsers without container queries). */
  .ev-list { flex: 1; min-height: 0; overflow-y: auto; padding: 0 8px 8px; max-height: 60vh; }
  /* Only while it has something to scroll: a short list (a kind chosen) that
     kept a swipe to itself would leave the page stuck under the finger. */
  .ev-list.scrolls { overscroll-behavior: contain; }
  .side { border-top: 1px solid var(--divider-color); }
  .ev-day { position: sticky; top: 0; z-index: 1; padding: 8px 4px 4px; font-size: 12px; font-weight: 500;
    color: var(--secondary-text-color); background: var(--ha-card-background, var(--card-background-color, var(--primary-background-color))); }
  .ev { display: grid; grid-template-columns: 112px minmax(0, 1fr); column-gap: 10px; align-items: center; width: 100%;
    text-align: left; border: 1px solid transparent; border-radius: 10px; padding: 5px; margin-bottom: 2px; min-height: 0;
    white-space: normal; }
  .ev:hover { background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
  .ev.on { border-color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 14%, transparent); }
  .thumb { position: relative; width: 112px; aspect-ratio: 16 / 9; border-radius: 6px; overflow: hidden;
    background: var(--secondary-background-color); display: flex; align-items: center; justify-content: center; }
  .thumb ha-icon { color: var(--secondary-text-color); --mdc-icon-size: 22px; opacity: .6; }
  .thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .thumb img.bad { display: none; }
  .thumb .dur { position: absolute; right: 3px; bottom: 3px; font-size: 10px; padding: 0 4px; border-radius: 3px;
    color: #fff; background: rgba(0,0,0,.6); font-variant-numeric: tabular-nums; }
  .evt { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .evt .n { font-size: 14px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .evt .m { font-size: 12px; color: var(--secondary-text-color); display: flex; align-items: center; gap: 5px;
    font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; }
  .evt .m i { width: 8px; height: 8px; border-radius: 50%; background: var(--cam); flex: none; }
  .evt .c { font-size: 12px; color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ev-foot { padding: 10px 4px; font-size: 12px; color: var(--secondary-text-color); text-align: center; }
  /* Smart search (Frigate) and the kinds to show. */
  .ev-tools { display: flex; flex-direction: column; gap: 6px; padding: 0 8px 6px; }
  .ev-search { display: flex; align-items: center; gap: 6px; padding: 0 4px 0 10px; border: 1px solid var(--divider-color);
    border-radius: 18px; min-height: 34px; }
  .ev-search:focus-within { border-color: var(--primary-color); }
  .ev-search ha-icon { --mdc-icon-size: 18px; color: var(--secondary-text-color); flex: none; }
  .ev-search input { flex: 1; min-width: 0; border: none; outline: none; background: transparent; font: inherit; font-size: 14px;
    color: var(--primary-text-color); padding: 6px 0; }
  .ev-sq { display: flex; align-items: center; gap: 4px; font-size: 13px; color: var(--secondary-text-color); padding-left: 4px; }
  .ev-sq .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ev-sq button.icon { width: 28px; min-width: 28px; height: 28px; --mdc-icon-size: 18px; }
  .ev-kinds { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; }
  .ev-kinds::-webkit-scrollbar { display: none; }
  .ev-kinds button { min-height: 28px; font-size: 13px; padding: 0 10px; flex: none; color: var(--secondary-text-color); }
  .ev-kinds button.on { color: var(--primary-text-color); }
  .ev-kinds .n { font-size: 11px; opacity: .7; font-variant-numeric: tabular-nums; }
  .evrow { position: relative; }
  .evrow .sim { position: absolute; right: 4px; bottom: 4px; width: 28px; min-width: 28px; height: 28px; --mdc-icon-size: 18px;
    color: var(--secondary-text-color); opacity: 0; pointer-events: none; }
  .evrow:hover .sim, .evrow .sim:focus-visible { opacity: 1; pointer-events: auto; }
  .evrow.has-sim .evt { padding-right: 28px; }
  @media (hover: none) { .evrow .sim { opacity: .75; pointer-events: auto; } }
`;

/**
 * Pinch / pan / double-tap zoom on one cell. The transform goes on the
 * viewport element (video + still) inside the cell, which clips it.
 * At 1x the cell lets the page scroll vertically (touch-action: pan-y);
 * zoomed in, it takes every gesture for panning.
 */
class Zoom {
  constructor(host, target, onTap) {
    this.host = host;
    this.target = target;
    this.onTap = onTap;
    this.s = 1;
    this.tx = 0;
    this.ty = 0;
    this.pts = new Map();
    this.lastTap = null;
    host.addEventListener("pointerdown", (e) => this._down(e));
    host.addEventListener("pointermove", (e) => this._move(e));
    host.addEventListener("pointerup", (e) => this._up(e));
    host.addEventListener("pointercancel", (e) => this._cancel(e));
    host.addEventListener("wheel", (e) => this._wheel(e), { passive: false });
  }

  reset() {
    this.s = 1;
    this.tx = this.ty = 0;
    this._apply();
  }

  /** Double-tap: zoom to 2.5x around the point, or back to 1x. */
  toggleAt(clientX, clientY) {
    if (this.s > 1) return this.reset();
    const m = this._rel(clientX, clientY);
    this.s = 2.5;
    this.tx = m.x * (1 - this.s);
    this.ty = m.y * (1 - this.s);
    this._apply();
  }

  // Point relative to the host's centre (the transform origin).
  _rel(x, y) {
    const r = this.host.getBoundingClientRect();
    return { x: x - r.left - r.width / 2, y: y - r.top - r.height / 2 };
  }

  _apply() {
    const w = this.host.clientWidth;
    const h = this.host.clientHeight;
    const mx = ((this.s - 1) * w) / 2;
    const my = ((this.s - 1) * h) / 2;
    this.tx = clamp(this.tx, -mx, mx);
    this.ty = clamp(this.ty, -my, my);
    this.target.style.transform = this.s > 1 ? `translate(${this.tx}px, ${this.ty}px) scale(${this.s})` : "";
    this.host.classList.toggle("zoomed", this.s > 1);
  }

  _gestureStart() {
    const p = [...this.pts.values()];
    this.g = { s: this.s, tx: this.tx, ty: this.ty, p };
    if (p.length >= 2) {
      this.g.d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1;
      this.g.m = this._rel((p[0].x + p[1].x) / 2, (p[0].y + p[1].y) / 2);
    }
  }

  _down(e) {
    if (e.target.closest("button") || (e.pointerType === "mouse" && e.button !== 0)) return;
    this.pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (this.pts.size === 1) this.tap = { x: e.clientX, y: e.clientY, t: Date.now(), moved: false };
    else this.tap = null; // a second finger: not a tap
    // At 1x a single finger may be a page scroll: leave it to the browser.
    if (this.pts.size >= 2 || this.s > 1) this.host.setPointerCapture?.(e.pointerId);
    this._gestureStart();
  }

  _move(e) {
    if (!this.pts.has(e.pointerId)) return;
    this.pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (this.tap && Math.hypot(e.clientX - this.tap.x, e.clientY - this.tap.y) > 10) this.tap.moved = true;
    const p = [...this.pts.values()];
    const g = this.g;
    if (p.length >= 2 && g.d) {
      const d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
      const m = this._rel((p[0].x + p[1].x) / 2, (p[0].y + p[1].y) / 2);
      this.s = clamp((g.s * d) / g.d, 1, ZOOM_MAX);
      // Keep the content under the fingers' midpoint under it.
      const k = this.s / g.s;
      this.tx = m.x - (g.m.x - g.tx) * k;
      this.ty = m.y - (g.m.y - g.ty) * k;
      this._apply();
    } else if (p.length === 1 && this.s > 1 && g.p[0]) {
      this.tx = g.tx + (p[0].x - g.p[0].x);
      this.ty = g.ty + (p[0].y - g.p[0].y);
      this._apply();
    }
  }

  _up(e) {
    if (!this.pts.delete(e.pointerId)) return;
    const tap = this.tap;
    if (this.pts.size === 0 && tap && !tap.moved && Date.now() - tap.t < 350) {
      const double = this.lastTap && Date.now() - this.lastTap.t < 320 && Math.hypot(e.clientX - this.lastTap.x, e.clientY - this.lastTap.y) < 40;
      this.lastTap = double ? null : { x: e.clientX, y: e.clientY, t: Date.now() };
      this.onTap(double, e.clientX, e.clientY);
    }
    this.tap = null;
    this._gestureStart(); // remaining finger continues as a pan
  }

  _cancel(e) {
    this.pts.delete(e.pointerId);
    this.tap = null;
    this._gestureStart();
  }

  _wheel(e) {
    // Only in fullscreen: elsewhere the wheel scrolls the dashboard.
    if (!this.host.closest(".stage")?.matches(":fullscreen")) return;
    e.preventDefault();
    const m = this._rel(e.clientX, e.clientY);
    const s0 = this.s;
    this.s = clamp(this.s * Math.exp(-e.deltaY / 400), 1, ZOOM_MAX);
    const k = this.s / s0;
    this.tx = m.x - (m.x - this.tx) * k;
    this.ty = m.y - (m.y - this.ty) * k;
    this._apply();
  }
}

// ---- SS's stream -----------------------------------------------------------

// Live: seconds of video held ahead of the playhead, the jitter margin
// (Wi-Fi cameras deliver frames in bursts). Latency is about this.
const LIVE_TARGET = 0.8;
const LIVE_JUMP = 2.5; // further behind than target + this: jump to the edge
const QUEUE_MAX = 90; // fragments waiting to be appended: drop to the next keyframe
// Recordings: SS sends them at the pace they play (at a higher speed with the
// timestamps squeezed, so the video itself plays at about 1x). Seconds of video:
const PLAY_START = 0.3; // buffered before it starts
const PLAY_TARGET = 1; // less than this ahead: play a little slower to build it up
const PLAY_HIGH = 4; // more than this ahead (paused, or a slow decoder): SS pauses
const SEEK_TIMEOUT_MS = 10_000; // no footage from SS by then: reconnect
const KEYFRAME_MAX = 6; // seconds: the longest a camera goes between keyframes
// SS plays a hole inside a recording file (the camera dropped out for a
// while) as that much time with nothing sent. Silent this long: ask for 16x
// until frames come again (a 29 s hole then takes 0.3 s).
const HOLE_MS = 1500;
const SEEK_SETTLE_MS = 1000; // after a jump, by when SS surely has it
const PACE_SETTLE_MS = 1500; // after a change of speed, by when frames come at the new one
// A follower asks SS for this many seconds (times the speed) past the
// master's time, and holds its first frame until the master gets there:
// SS starts at the keyframe before the time asked for, so asking for the
// master's time exactly would leave it behind, and SS never sends faster than
// the speed asked for, so behind can't be caught up.
const FOLLOW_LEAD = 1;

/** Parse one SS stream message: 4-byte header end, query-string header, payload. */
function readStreamMsg(buf) {
  const b = new Uint8Array(buf);
  if (b.length <= 4) return null;
  const end = ((b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]) >>> 0;
  const head = {};
  for (const pair of String.fromCharCode(...b.subarray(4, end)).split("&")) {
    const i = pair.indexOf("=");
    if (i > 0) head[pair.slice(0, i)] = pair.slice(i + 1);
  }
  return { head, data: b.subarray(end) };
}

function findBox(buf, tag) {
  const t = [...tag].map((c) => c.charCodeAt(0));
  for (let i = 4; i + 4 <= buf.length; i++)
    if (buf[i] === t[0] && buf[i + 1] === t[1] && buf[i + 2] === t[2] && buf[i + 3] === t[3]) return i;
  return -1;
}

/** RFC 6381 codec string from an init segment (moov with hvcC / avcC), or null. */
function codecOf(moov) {
  const find = (tag) => findBox(moov, tag);
  const hex = (n) => n.toString(16).toUpperCase();
  let i = find("hvcC");
  if (i > 0) {
    const c = moov.subarray(i + 4);
    const space = ["", "A", "B", "C"][c[1] >> 6];
    const tier = c[1] & 0x20 ? "H" : "L";
    const profile = c[1] & 0x1f;
    let compat = ((c[2] << 24) | (c[3] << 16) | (c[4] << 8) | c[5]) >>> 0;
    let rev = 0; // the flags are written bit-reversed
    for (let k = 0; k < 32; k++) rev = (rev << 1) | ((compat >>> k) & 1);
    const cons = [...c.subarray(6, 12)];
    while (cons.length && !cons.at(-1)) cons.pop();
    const fourcc = find("hev1") > 0 ? "hev1" : "hvc1";
    return [fourcc, `${space}${profile}`, hex(rev >>> 0), `${tier}${c[12]}`, ...cons.map(hex)].join(".");
  }
  i = find("avcC");
  if (i > 0) {
    const c = moov.subarray(i + 4);
    return "avc1." + [c[1], c[2], c[3]].map((x) => x.toString(16).padStart(2, "0")).join("");
  }
  return null;
}

/** RFC 6381 codec string of an audio init segment's sample entry (AAC's object type from its esds). */
function audioCodecOf(moov) {
  const i = findBox(moov, "stsd");
  if (i < 0) return "mp4a.40.2";
  const entry = String.fromCharCode(...moov.subarray(i + 16, i + 20));
  const named = { Opus: "opus", fLaC: "flac", "ac-3": "ac-3", "ec-3": "ec-3", ".mp3": "mp3", ulaw: "ulaw", alaw: "alaw" };
  if (named[entry]) return named[entry];
  if (entry !== "mp4a") return entry.trim();
  // esds: ES_Descriptor (3) > DecoderConfigDescriptor (4): object type, then
  // DecoderSpecificInfo (5): AAC's audio object type in its first 5 bits.
  const e = findBox(moov, "esds");
  if (e < 0) return "mp4a.40.2";
  let k = e + 8; // past "esds" and version/flags
  const descriptor = (tag) => {
    while (k < moov.length && moov[k] !== tag) k++;
    k++;
    while (k < moov.length && moov[k] & 0x80) k++; // the length's continuation bytes
    return ++k < moov.length;
  };
  if (!descriptor(0x03)) return "mp4a.40.2";
  k += 3; // ES_ID, flags
  if (!descriptor(0x04)) return "mp4a.40.2";
  const oti = moov[k];
  if (oti !== 0x40) return `mp4a.${oti.toString(16).padStart(2, "0")}`;
  k += 13; // object type, stream type, buffer size, bitrates
  if (!descriptor(0x05)) return "mp4a.40.2";
  let aot = moov[k] >> 3;
  if (aot === 31) aot = 32 + (((moov[k] & 7) << 3) | (moov[k + 1] >> 5)); // the escape: 6 more bits
  return `mp4a.40.${aot || 2}`;
}

/**
 * One MediaSource + SourceBuffer fed fragment by fragment.
 * onAppended(meta): a fragment pushed with meta is in the buffer.
 * trimEnd(t): how much of the played part may go, for a removal at playhead
 *   t (remove() takes everything up to the next keyframe: video passes the
 *   last keyframe before t - 8).
 * onError(): the source failed (a decode error, or it was closed under us).
 */
class MseSink {
  constructor(el, { onAppended, trimEnd, onError } = {}) {
    this.el = el;
    this.onAppended = onAppended;
    this.trimEnd = trimEnd ?? ((t) => t - 8);
    this.onError = onError;
    this.queue = [];
    this.sb = null;
    this.pending = null;
    this.ms = new MSE();
    // A ManagedMediaSource only streams to an element that can't be AirPlayed.
    if (MSE !== window.MediaSource) el.disableRemotePlayback = true;
    this.url = URL.createObjectURL(this.ms);
    el.src = this.url;
    this.opened = new Promise((r) => this.ms.addEventListener("sourceopen", r, { once: true }));
    this.ms.addEventListener("sourceended", () => this.fail());
    this.ms.addEventListener("sourceclose", () => this.fail());
  }

  fail() {
    if (this.closed) return;
    this.closed = true;
    this.queue = [];
    this.onError?.();
  }

  /** Create the buffer and append the init segment (ftyp, moov). */
  async init(parts, mime) {
    await this.opened;
    if (this.closed || this.sb || this.ms.readyState !== "open") return false;
    try {
      this.ms.duration = Infinity;
      this.sb = this.ms.addSourceBuffer(mime);
    } catch (e) {
      this.fail();
      return false;
    }
    // SS's fragment timestamps start over on every jump and don't survive
    // reconnects or Wi-Fi hiccups; appended in order, each fragment follows
    // the last.
    this.sb.mode = "sequence";
    this.sb.addEventListener("updateend", () => this.pump());
    this.sb.addEventListener("error", () => this.fail());
    // Ahead of anything queued while the source was opening.
    this.queue.unshift(...parts.map((part) => [part, null]));
    this.pump();
    return true;
  }

  /** Queue a fragment (meta: reported once appended), an init segment (no meta) or a buffer call. */
  push(data, meta) {
    if (this.closed) return;
    this.queue.push([data, meta]);
    this.pump();
  }

  /** Forget the fragments not appended yet (init segments stay). */
  dropQueued() {
    this.queue = this.queue.filter(([, meta]) => !meta);
  }

  pump() {
    const sb = this.sb;
    if (!sb || sb.updating || this.closed || this.ms.readyState !== "open") return;
    if (this.pending) {
      const meta = this.pending;
      this.pending = null;
      this.onAppended?.(meta);
    }
    // Drop what the playhead left behind, now and then.
    const b = sb.buffered;
    const t = this.el.currentTime;
    if (b.length && t - b.start(0) > 20) {
      const end = this.trimEnd(t);
      if (end > b.start(0) + 1) {
        sb.remove(b.start(0), end);
        return;
      }
    }
    for (;;) {
      const next = this.queue.shift();
      if (!next) return;
      try {
        if (typeof next[0] === "function") {
          next[0](sb);
          continue;
        }
        this.pending = next[1];
        sb.appendBuffer(next[0]);
      } catch (e) {
        this.pending = null;
        if (e.name === "QuotaExceededError") {
          this.dropQueued();
          this.overflow = true; // the feed starts over at a keyframe
        } else this.fail();
      }
      return;
    }
  }

  end() {
    const b = this.sb?.buffered;
    return b?.length ? b.end(b.length - 1) : 0;
  }

  /** Media time m is in the buffer. */
  has(m) {
    const b = this.sb?.buffered;
    for (let i = 0; i < (b?.length ?? 0); i++) if (m >= b.start(i) && m < b.end(i)) return true;
    return false;
  }

  close() {
    this.closed = true;
    this.queue = [];
    URL.revokeObjectURL(this.url);
  }
}

/**
 * A media element fed from SS's stream: its sink, and for each fragment in
 * it [media start, media end, wall time of its first frame, starts a run].
 * A run is footage SS sent in one go: a jump starts a new one.
 */
class Track {
  constructor(el, { onAppended, onError }) {
    this.el = el;
    this.map = [];
    this.keys = []; // media start of each keyframe fragment
    this.onAppended = onAppended;
    this.sink = new MseSink(el, {
      onAppended: (meta) => this.appended(meta),
      // Audio has no keyframes to keep: every fragment starts clean.
      trimEnd: (t) => (this.keys.length ? this.keyBefore(t - 8) : t - 8),
      onError,
    });
  }

  appended(meta) {
    const end = this.sink.end();
    // In sequence mode a fragment starts where the previous one ended.
    const start = this.map.at(-1)?.[1] ?? Math.max(0, end - 0.1);
    this.map.push([start, end, meta.wall, !!meta.run]);
    if (meta.key) this.keys.push(start);
    if (this.map.length > 4000) this.map.splice(0, 1000);
    if (this.keys.length > 600) this.keys.splice(0, 200);
    this.onAppended?.(meta, start, end);
  }

  /** The last keyframe at or before media time t (or -Infinity). */
  keyBefore(t) {
    for (let i = this.keys.length - 1; i >= 0; i--) if (this.keys[i] <= t) return this.keys[i];
    return -Infinity;
  }

  /** Index of the fragment holding media time m (the last starting at or before it), or -1. */
  at(m) {
    const map = this.map;
    if (!map.length || m < map[0][0]) return map.length ? 0 : -1;
    let lo = 0;
    let hi = map.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (map[mid][0] <= m) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }

  /** Footage seconds per media second around fragment i: 1, or the speed SS squeezed it by. */
  ratio(i) {
    for (const j of [i, i - 1]) {
      const a = this.map[j];
      const b = this.map[j + 1];
      if (!a || !b || b[3] || b[0] - a[0] < 0.001) continue;
      const r = (b[2] - a[2]) / (b[0] - a[0]);
      if (r > 0 && r < 40) return r;
    }
    return 1;
  }

  /** Wall time shown at media time m (from the frames' own timestamps). */
  wall(m) {
    const i = this.at(m);
    if (i < 0) return null;
    const [start, , wall] = this.map[i];
    return wall + Math.max(0, m - start) * this.ratio(i);
  }

  /** Media time of wall time t in the current run, if it's in the buffer; else null. */
  mediaAt(t) {
    const map = this.map;
    for (let i = map.length - 1; i >= 0; i--) {
      const [start, end, wall, run] = map[i];
      if (wall <= t) {
        const m = start + (t - wall) / this.ratio(i);
        // Past this fragment's end: t fell between two frames further apart
        // than the footage (a gap), or isn't here yet.
        if (m > end + 0.01) return null;
        return this.sink.has(m) ? m : null;
      }
      if (run) return null;
    }
    return null;
  }

  /** Recent footage seconds per media second (the speed SS squeezes by), or 1. */
  recentRatio() {
    const map = this.map;
    const n = map.length;
    if (n < 2) return 1;
    let i = n - 1;
    while (i > 0 && n - i < 30 && !map[i][3]) i--;
    const a = map[i];
    const b = map[n - 1];
    const r = a && b[0] - a[0] > 0.3 ? (b[2] - a[2]) / (b[0] - a[0]) : 1;
    return r > 0 && r < 40 ? r : 1;
  }
}

/**
 * A camera on SS's stream (a WebSocket relayed by HA), played through MSE
 * into the player's <video>: live (at == null), or its recordings from wall
 * time `at` on, which seekTo() moves within the same connection.
 *
 * Live keeps LIVE_TARGET seconds ahead of the playhead, gently (playback
 * speed) or, when far behind, by jumping. Recordings play at the pace SS
 * sends them, a little slower while less than PLAY_TARGET is ahead; when
 * more than PLAY_HIGH piles up SS is told to pause. A follower's pace is set by
 * follow() (`nudge`) instead.
 *
 * Sound, only when wanted, goes through its own <audio>, kept on the
 * video's wall time: a video that waits for audio would stall on every burst.
 */
class StreamFeed {
  constructor(player, url, { at = null, speed = 1, onStart, onLanded, onEnd }) {
    this.player = player;
    if (player.audioUnplayable) {
      player.audioUnplayable = null; // until this stream's audio says otherwise
      player.card?._syncMuteIcon();
    }
    this.video = player.video;
    this.live = at == null;
    this.speed = this.live ? 1 : speed;
    this.onStart = onStart;
    this.onLanded = onLanded;
    this.onEnd = onEnd;
    this.nudge = 1;
    this.gen = 0; // bumped by every jump; fragments from before it don't count
    this.run = true; // the next video fragment starts a run
    this.waitKey = true; // dropping until the next keyframe
    this.lastWall = null; // of the last video frame received
    this.seeking = null; // a jump asked for and not there yet: {t, prior, at}
    this.wantLanding = !this.live; // the next run's first fragment is where playback starts
    this.landing = null; // that fragment, until enough follows it: {start, wall}
    this.ssPaused = false;
    this.bridging = false; // at 16x over a hole
    this.opened = this.lastFrameAt = this.jumpedAt = Date.now();
    this.paceAfter = 0; // until then (a speed change settling) pace is taken as 1
    this.audio = null;
    this.audioInit = null;
    this.track = new Track(this.video, {
      onAppended: (meta, start, end) => this.appended(meta, start, end),
      onError: () => this.end("error"),
    });
    // Attaching reset the rate to the card's playback speed; the stream sets its own.
    this.video.defaultPlaybackRate = this.video.playbackRate = 1;
    this.events = new AbortController();
    const on = (type, fn) => this.video.addEventListener(type, fn, { signal: this.events.signal });
    on("error", () => this.end("error"));
    on("pause", () => this.audio?.el.pause());
    on("play", () => this.resumeAudio());
    this.ws = new WebSocket(url);
    this.ws.binaryType = "arraybuffer";
    this.ws.onopen = () => this.speed !== 1 && this.send(`speed=${this.speed}`);
    this.ws.onmessage = (e) => this.message(e.data);
    this.ws.onclose = () => this.end("closed");
    this.ws.onerror = () => {};
    this.keepAlive = setInterval(() => this.send("keepAlive"), 10000);
    this.control = setInterval(() => this.steer(), 250);
  }

  get started() {
    return !!this.startedAt;
  }

  get follower() {
    return this.player.card._master !== this.player;
  }

  send(s) {
    if (this.ws.readyState === 1) this.ws.send(s);
  }

  /** The stream is over (closed, refused, undecodable, stuck); the player decides what next. */
  end(why, detail) {
    if (this.closed || this.ended) return;
    this.ended = true;
    this.onEnd?.(why, detail);
  }

  /** Recordings: go to wall time t (SS starts at the keyframe before it). */
  seekTo(t) {
    this.gen++;
    // Targets of jumps asked for before this one and not seen yet.
    const prior = this.seeking ? [...this.seeking.prior, this.seeking.t].slice(-4) : [];
    this.seeking = { t, prior, at: Date.now() };
    this.wantLanding = true;
    this.landing = null;
    this.track.sink.dropQueued();
    this.audio?.track.sink.dropQueued();
    if (this.audio) this.audio.started = false;
    // SS paused sends just the first frame of the new place.
    if (this.ssPaused) this.send("pause=false");
    this.ssPaused = false;
    this.endBridge();
    this.send(`time=${Math.floor(t)}`);
    this.lastFrameAt = this.jumpedAt = Date.now();
  }

  setSpeed(speed) {
    if (this.live || speed === this.speed) return;
    this.speed = speed;
    this.bridging = false;
    this.send(`speed=${speed}`);
    this.paceAfter = Date.now() + PACE_SETTLE_MS;
  }

  /** Back to the chosen speed after a hole; what SS sent at 16x meanwhile starts a run. */
  endBridge() {
    if (!this.bridging) return;
    this.bridging = false;
    this.send(`speed=${this.speed}`);
    this.run = true;
    this.paceAfter = Date.now() + PACE_SETTLE_MS;
  }

  message(buf) {
    const msg = readStreamMsg(buf);
    if (!msg || this.closed || this.ended) return;
    const { head, data } = msg;
    if (head.close) return this.end("closed");
    if (head.vdoCodec || head.adoCodec) {
      // First, and again whenever SS opens another recording file (new init segments follow).
      // Anything but (M)JPEG is checked against the browser on its moov: SS's
      // names for H.264/H.265 variants aren't all known.
      if (head.vdoCodec && /JPEG/i.test(head.vdoCodec)) return this.end("codec", head.vdoCodec);
      return;
    }
    const video = head.mediaType === "1";
    const box = String.fromCharCode(...data.subarray(4, 8));
    if (box === "ftyp") {
      this[video ? "vFtyp" : "aFtyp"] = data.slice();
      return;
    }
    if (box === "moov") return video ? this.videoMoov(data.slice()) : this.audioMoov(data.slice());
    const wall = Math.min(Number(head.msec) / 1000, nowS());
    if (!video) return this.audioFragment(data, wall);
    if (!this.track.sink.sb) return;
    const key = head.key === "1";
    const last = this.lastWall;
    this.lastWall = wall;
    this.lastFrameAt = Date.now();
    this.endBridge();
    if (this.seeking) {
      // Until SS gets there, the old place keeps coming, frame after frame,
      // and so may an earlier jump still on its way. Ours starts with a
      // keyframe: shortly before t, or later when nothing was recorded at t.
      const s = this.seeking;
      if (!key) return;
      const near = (t) => wall >= t - KEYFRAME_MAX && wall <= t + 0.5;
      const jumped = last == null || Math.abs(wall - last) > Math.max(0.5, 0.3 * this.speed);
      // Once SS has surely had the command, what comes is the new place
      // (however far back its keyframe was, or wherever an old jump went).
      // The old place arriving right at t is as good as the new one.
      const late = Date.now() - s.at > SEEK_SETTLE_MS;
      if (near(s.t) ? !(jumped || late || wall >= s.t - 0.5) : !jumped || (!late && (wall < s.t || s.prior.some(near)))) return;
      this.seeking = null;
      this.run = true;
      this.waitKey = false;
    }
    // Live and paused, or the browser fell behind: skip to the next keyframe
    // (a skip starts a run: the footage isn't continuous across it).
    if (this.live && this.video.paused && this.started) this.waitKey = this.run = true;
    if (this.track.sink.queue.length >= QUEUE_MAX || this.track.sink.overflow) {
      this.track.sink.dropQueued();
      this.track.sink.overflow = false;
      this.waitKey = this.run = true;
    }
    // (Before the first frame the video is paused: that's not a pause.)
    if (this.waitKey && !(key && (!this.live || !this.started || !this.video.paused))) return;
    this.waitKey = false;
    // A gap SS skipped over without a jump asked for starts a run too
    // (frames are at most ~0.15 s of footage apart, times the speed).
    const run = this.run || (last != null && Math.abs(wall - last) > Math.max(1, 0.5 * this.speed));
    this.run = false;
    this.track.sink.push(data.slice(), { wall, key, run, gen: this.gen });
  }

  /** A codec string and MIME type the browser takes for this moov, relabelling hev1 as hvc1 if that's what it takes. */
  videoType(moov) {
    let codec = codecOf(moov);
    let mime = `video/mp4; codecs="${codec}"`;
    if (!MSE.isTypeSupported(mime) && codec?.startsWith("hev1")) {
      // Some browsers only take HEVC as hvc1; SS's hvcC carries the
      // parameter sets, so relabelling the sample entry is enough.
      const alt = codec.replace("hev1", "hvc1");
      if (MSE.isTypeSupported(`video/mp4; codecs="${alt}"`)) {
        const i = findBox(moov, "hev1");
        if (i > 0) moov.set([0x68, 0x76, 0x63, 0x31], i);
        codec = alt;
        mime = `video/mp4; codecs="${alt}"`;
      }
    }
    return codec && MSE.isTypeSupported(mime) ? { codec, mime } : { codec: null, detail: codec };
  }

  videoMoov(moov) {
    const type = this.videoType(moov);
    if (!type.codec) return this.end("codec", type.detail);
    const sink = this.track.sink;
    if (!this.codec) {
      this.codec = type.codec;
      sink.init([this.vFtyp, moov], type.mime);
      return;
    }
    // Another recording file (or the camera's settings changed): its init
    // segment goes in line, switching the decoder if it must.
    if (type.codec !== this.codec) {
      if (!sink.sb?.changeType) return this.end("error");
      this.codec = type.codec;
      sink.push((sb) => sb.changeType(type.mime), null);
    }
    sink.push(this.vFtyp, null);
    sink.push(moov, null);
  }

  appended(meta, start, end) {
    if (meta.gen !== this.gen) return;
    if (this.live) {
      if (!this.started && end > LIVE_TARGET) {
        this.startedAt = Date.now();
        this.video.currentTime = Math.max(0, end - LIVE_TARGET);
        this.onStart?.();
      }
      return;
    }
    if (this.wantLanding && meta.run) {
      this.wantLanding = false;
      this.landing = { start, wall: meta.wall };
    }
    const land = this.landing;
    if (!land || end - land.start < PLAY_START) return;
    this.landing = null;
    this.video.currentTime = land.start;
    if (!this.started) {
      this.startedAt = Date.now();
      this.onStart?.(land.wall);
    } else this.onLanded?.(land.wall);
    this.alignAudio();
  }

  /** Wall time shown at media time m. */
  wall(m) {
    return this.track.wall(m);
  }

  /** Where wall time t is in what's buffered of the current run, or null. */
  mediaAt(t) {
    return this.seeking || this.wantLanding || this.landing ? null : this.track.mediaAt(t);
  }

  steer() {
    const v = this.video;
    const sink = this.track.sink;
    // Asked for a place (or opened) and nothing to play there yet: start over.
    const waiting = this.seeking || this.wantLanding || this.landing;
    if (!this.started ? Date.now() - this.opened > 15000 : waiting && Date.now() - this.jumpedAt > SEEK_TIMEOUT_MS)
      return this.end("stalled");
    if (!this.started || this.seeking || this.wantLanding || this.landing) return;
    const ahead = sink.end() - v.currentTime;
    let rate = 1;
    if (this.live) {
      if (ahead > LIVE_TARGET + LIVE_JUMP && !v.paused) v.currentTime = sink.end() - LIVE_TARGET;
      else if (ahead > LIVE_TARGET + 0.3) rate = 1.1;
      else if (ahead < LIVE_TARGET - 0.3) rate = 0.93;
    } else {
      // Enough piled up (paused, or a decoder that can't keep up): SS waits.
      const full = ahead > PLAY_HIGH || sink.queue.length > 30;
      if (full !== this.ssPaused && (full || (ahead < PLAY_HIGH - 1 && sink.queue.length < 10))) {
        this.ssPaused = full;
        this.send(`pause=${full}`);
        this.lastFrameAt = Date.now();
      }
      if (!this.ssPaused && !this.bridging && this.speed < 16 && Date.now() - this.lastFrameAt > HOLE_MS) {
        this.bridging = true;
        this.send("speed=16");
      }
      // SS sends `speed` seconds of footage a second (only 1 once it's up to
      // the present); played at this rate, the video keeps pace with it.
      // Right after a change of speed the frames still arriving were sent
      // at the old one. Never faster: more than enough ahead just makes SS wait.
      const footage = this.lastWall > nowS() - 5 ? 1 : this.speed;
      const pace = Date.now() < this.paceAfter ? 1 : clamp(footage / this.track.recentRatio(), 0.5, 1.25);
      if (this.follower) rate = pace * (ahead < 0.25 ? Math.min(this.nudge, 0.9) : this.nudge);
      else rate = pace * clamp(1 + (ahead - PLAY_TARGET) * 0.15, 0.85, 1);
      rate = Math.round(rate * 100) / 100;
    }
    if (!v.paused && !v.seeking && v.playbackRate !== rate) v.playbackRate = rate;
    this.steerAudio();
  }

  // ---- sound ----

  /** Sound on (the master, unmuted, at 1x) or off. */
  setAudio(on) {
    if (on && !this.audio && !this.player.audioUnplayable) this.startAudio();
    if (!on && this.audio) this.stopAudio();
  }

  /**
   * Runs inside the tap that unmuted (or picked the master): play() is asked
   * for now, while the browser allows it (Safari needs the tap), and starts
   * once there is sound to play.
   */
  startAudio() {
    const el = document.createElement("audio");
    const a = (this.audio = { el, started: false, inited: false });
    a.track = new Track(el, {
      onAppended: () => a.started || this.alignAudio(),
      // Failing before it played (addSourceBuffer threw, the init segment
      // was refused): unplayable here, and said so; after: stopped.
      onError: () => this.audio === a && (a.started ? this.stopAudio() : this.audioUnplayableNow(a.codec ?? "?")),
    });
    el.play().catch(() => {});
    if (this.audioInit) this.initAudio();
  }

  audioMoov(moov) {
    const reinit = !!this.audioInit;
    this.audioInit = [this.aFtyp, moov];
    const a = this.audio;
    if (!a) return;
    if (!a.inited) return this.initAudio();
    if (!reinit) return;
    // The next recording file. Its audio may be another codec (the camera's
    // setting changed): the SourceBuffer is told first, if the browser takes it.
    const codec = audioCodecOf(moov);
    if (codec !== a.codec) {
      const mime = `audio/mp4; codecs="${codec}"`;
      if (!MSE.isTypeSupported(mime) || !a.track.sink.sb?.changeType) return this.audioUnplayableNow(codec);
      a.codec = codec;
      a.track.sink.push((sb) => sb.changeType(mime), null);
    }
    for (const part of this.audioInit) a.track.sink.push(part, null);
  }

  audioUnplayableNow(codec) {
    this.player.audioUnplayable = codec;
    this.player.card._syncMuteIcon();
    this.stopAudio();
  }

  async initAudio() {
    const a = this.audio;
    if (!a || a.inited) return;
    a.inited = true;
    // Whatever the camera sends: the browser says whether it can play it.
    const codec = (a.codec = audioCodecOf(this.audioInit[1]));
    const mime = `audio/mp4; codecs="${codec}"`;
    // (Said to be playable but refused all the same, addSourceBuffer throwing
    // or the init segment refused: the track's onError says so.)
    if (!MSE.isTypeSupported(mime)) return this.audioUnplayableNow(codec);
    await a.track.sink.init(this.audioInit, mime);
  }

  audioFragment(data, wall) {
    const a = this.audio;
    if (!a?.track.sink.sb || this.seeking || this.speed !== 1 || this.bridging || Date.now() < this.paceAfter) return;
    // Live and paused: nothing to keep. Old-place sound after a jump: dropped.
    if (this.live && this.video.paused) return;
    if (this.lastWall == null || Math.abs(wall - this.lastWall) > 3) return;
    const sink = a.track.sink;
    if (sink.queue.length >= QUEUE_MAX) sink.dropQueued();
    const last = a.track.map.at(-1);
    sink.push(data.slice(), { wall, run: !last || Math.abs(wall - last[2]) > 3 });
  }

  /** Put the sound where the video is (when it has sound for that time). */
  alignAudio() {
    const a = this.audio;
    const v = this.video;
    if (!a || !this.started || this.seeking || this.wantLanding || this.landing || v.readyState < 3) return;
    const m = a.track.mediaAt(this.wall(v.currentTime));
    if (m == null) return;
    a.el.currentTime = m;
    a.started = true;
    if (!v.paused) a.el.play().catch(() => {});
  }

  resumeAudio() {
    const a = this.audio;
    if (!a?.started) return;
    this.alignAudio();
    a.el.play().catch(() => {});
  }

  /** Keep the sound on the video's wall time: a small drift by speed, a larger one by a jump. */
  steerAudio() {
    const a = this.audio;
    const v = this.video;
    if (!a) return;
    if (!a.started) return this.alignAudio();
    if (v.paused || a.el.paused || a.el.seeking || v.seeking) return;
    const diff = a.track.wall(a.el.currentTime) - this.wall(v.currentTime);
    if (Math.abs(diff) > 0.3) {
      this.alignAudio();
      return;
    }
    const rate = Math.round(v.playbackRate * (1 - clamp(diff * 0.5, -0.05, 0.05)) * 100) / 100;
    if (a.el.playbackRate !== rate) a.el.playbackRate = rate;
  }

  stopAudio() {
    const a = this.audio;
    if (!a) return;
    this.audio = null;
    a.track.sink.close();
    a.el.pause();
    a.el.removeAttribute("src");
    a.el.load();
  }

  close() {
    this.closed = true;
    this.events.abort();
    clearInterval(this.keepAlive);
    clearInterval(this.control);
    this.ws.onmessage = this.ws.onclose = this.ws.onopen = null;
    try {
      this.ws.close();
    } catch (e) {
      /* already closed */
    }
    this.stopAudio();
    this.track.sink.close();
  }
}

/**
 * Plays one camera: SS's stream (live, or the recordings from a time on)
 * through MSE; without MSE, the integration's HLS sessions over a window of
 * wall-clock time. Its loading veil, and the wall <-> media mapping.
 */
class Player {
  constructor(card, cameraId) {
    this.card = card;
    this.cameraId = cameraId;
    this.feed = null; // SS's stream, with MSE
    this.session = null; // {live, ws} on the stream; without MSE the last vod response
    this.mediaReady = false;
    this.target = nowS() - LIVE_LAG; // wall time we want / are at when nothing is playing
    this.seq = 0;
    this.loading = false;
    this.veilKind = null;
    this.retry = null;
    this.lead = 1; // follower: times FOLLOW_LEAD; doubled while it keeps landing behind

    const el = (this.el = document.createElement("div"));
    el.className = "cell";
    el.dataset.cell = cameraId;
    el.innerHTML = `
      <div class="vp"><video muted playsinline preload="auto" poster="${BLANK_POSTER}" disablepictureinpicture></video><canvas class="still"></canvas></div>
      ${VEIL_HTML}
      <div class="label">${esc(card._cameraName(cameraId))}</div>`;
    this.video = el.querySelector("video");
    this.still = el.querySelector(".still");
    this.veil = el.querySelector(".veil");
    this.zoom = new Zoom(el, el.querySelector(".vp"), (double, x, y) => card._onCellTap(this, double, x, y));

    const v = this.video;
    const isMaster = () => card._master === this;
    v.playbackRate = v.defaultPlaybackRate = card._rate;
    v.addEventListener("timeupdate", () => isMaster() && card._onTime());
    v.addEventListener("seeked", () => isMaster() && card._onTime());
    v.addEventListener("loadeddata", () => {
      this.mediaReady = true;
      if (isMaster()) card._onTime();
    });
    // Short stalls shouldn't flash the veil.
    v.addEventListener("waiting", () => {
      clearTimeout(this.bufTimer);
      this.bufTimer = setTimeout(() => {
        // Don't replace a veil that says more ("Loading", an error).
        if (!this.veilKind && (!v.paused || v.seeking)) this.setStatus("Buffering", "loading", this.subline(this.wall()));
      }, 400);
    });
    const ready = () => {
      clearTimeout(this.bufTimer);
      if (this.veilKind === "loading" && !this.loading) this.setStatus("");
      this.still.classList.remove("show");
    };
    v.addEventListener("playing", ready);
    // Paused: a frame is on screen once it can play / the seek landed.
    v.addEventListener("canplay", () => v.paused && ready());
    v.addEventListener("seeked", () => v.paused && ready());
    v.addEventListener("play", () => isMaster() && card._syncPlayIcon());
    v.addEventListener("pause", () => isMaster() && card._syncPlayIcon());
    // Followers don't continue on their own: follow() moves them.
    v.addEventListener("ended", () => isMaster() && this.continue());
  }

  subline(t) {
    return `${this.card._cameraName(this.cameraId)} · ${fmtDate(t)} ${fmtTime(t)}`;
  }

  setStatus(text, kind = "loading", sub = "", retry = null) {
    if (kind !== "loading" || !text) clearTimeout(this.bufTimer);
    clearTimeout(this.veilTimer);
    this.veilKind = text ? kind : null;
    this.retry = retry;
    setVeil(this.veil, text, kind, sub);
  }

  /** "Loading", unless it's there within 400 ms (a jump inside the stream usually is). */
  veilSoon(sub) {
    clearTimeout(this.veilTimer);
    this.veilTimer = setTimeout(() => this.loading && !this.veilKind && this.setStatus("Loading", "loading", sub), 400);
  }

  /** Keep the last frame on screen (it gets blurred) while the source changes. */
  freeze() {
    const v = this.video;
    if (v.readyState < 2 || !v.videoWidth) return;
    const c = this.still;
    const w = Math.min(640, v.videoWidth); // it's blurred anyway
    c.width = w;
    c.height = Math.round((w * v.videoHeight) / v.videoWidth);
    try {
      c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
      c.classList.add("show");
    } catch (e) {
      /* no still, just the dark veil */
    }
  }

  // ---- time mapping (HLS) -------------------------------------------------

  /** Playlist position for a wall time; inside a gap, the start of the next run. */
  wallToMedia(t) {
    for (const r of this.session?.runs ?? []) {
      if (t < r.wall_start) return r.media_start;
      if (t < r.wall_start + r.duration) return r.media_start + (t - r.wall_start);
    }
    return null;
  }

  /** Past the last run of a live session, media time continues that run. */
  wallToMediaLive(t) {
    const r = this.session?.runs?.at(-1);
    return r && t >= r.wall_start ? r.media_start + (t - r.wall_start) : null;
  }

  /** Exact playlist position of wall time t, or null if nothing was recorded then. */
  exactMedia(t) {
    const runs = this.session?.runs ?? [];
    for (const r of runs) {
      if (t >= r.wall_start && t < r.wall_start + r.duration) return r.media_start + (t - r.wall_start);
    }
    return this.session?.live ? this.wallToMediaLive(t) : null;
  }

  mediaToWall(m) {
    const runs = this.session?.runs ?? [];
    for (let i = 0; i < runs.length; i++) {
      const r = runs[i];
      if (m < r.media_start + r.duration || i === runs.length - 1) {
        return r.wall_start + Math.max(0, m - r.media_start);
      }
    }
    return null;
  }

  /** Playing, or about to (a load that will autoplay). */
  intendsPlay() {
    return this.loading ? !!this.autoplay : !this.video.paused;
  }

  wall() {
    if (this.feed) return (this.mediaReady && !this.loading && this.feed.wall(this.video.currentTime)) || this.target;
    // In a gap the media sits at the next run's start; the time is target.
    if (this.session && this.mediaReady && !this.loading && !this.gap) return this.mediaToWall(this.video.currentTime) ?? this.target;
    return this.target;
  }

  // ---- playback -----------------------------------------------------------

  destroyMedia() {
    if (this.feed) {
      this.feed.close();
      this.feed = null;
    }
    this.mediaReady = false;
    const v = this.video;
    v.pause();
    v.removeAttribute("src");
    v.load();
  }

  destroy() {
    this.seq++;
    this.goingLive = this.connecting = this.reconnecting = false;
    clearTimeout(this.bufTimer);
    clearTimeout(this.veilTimer);
    this.destroyMedia();
    this.session = null;
    this.el.remove();
  }

  play() {
    // Loading: play once it lands (the old media must stay paused).
    if (this.loading) this.autoplay = true;
    // In a gap (or nothing loaded): play from the next footage.
    else if (!this.session || this.gap) this.seek(this.target, true);
    else this.video.play().catch(() => {});
    this.card._syncPlayIcon();
  }

  pause() {
    this.autoplay = false;
    this.video.pause();
    this.card._syncPlayIcon();
  }

  seek(t, autoplay) {
    if (MSE) {
      if (!isLiveTime(t)) return this.stream(t, autoplay);
      // Already live (or connecting): just the play intent.
      if (this.feed?.live || this.goingLive) {
        this.autoplay = autoplay;
        if (!this.loading) autoplay ? this.video.play().catch(() => {}) : this.video.pause();
        return;
      }
      return this.stream(null, autoplay);
    }
    t = Math.min(t, nowS() - LIVE_LAG);
    const s = this.session;
    if (this.loading) {
      // Already on its way there: just take the newer play intent.
      if (Math.abs(t - this.target) < 0.5) {
        this.autoplay = autoplay;
        return;
      }
      // Otherwise the old media is still attached; seeking it would be
      // undone when the load lands.
      return this.load(t, autoplay);
    }
    // A live session's playlist keeps growing, so its end is "now".
    const end = s?.live ? nowS() - LIVE_LAG : (s?.end ?? 0) - 1;
    const from = s ? Math.min(s.start, s.reqStart ?? s.start) : 0;
    if (s && this.mediaReady && t >= from && t < end) {
      const m = this.wallToMedia(t) ?? (s.live ? this.wallToMediaLive(t) : null);
      if (m != null) {
        this.seq++; // cancels a pending continue()
        this.gap = false;
        if (this.veilKind && this.veilKind !== "loading") this.setStatus("");
        this.target = t;
        this.video.currentTime = m;
        if (this.card._master === this) this.card._paint(t);
        // A follower sent into a gap stays paused; follow() shows the gap.
        const isMaster = this.card._master === this;
        if (autoplay && (isMaster || this.exactMedia(t) != null)) this.video.play().catch(() => {});
        return;
      }
    }
    this.load(t, autoplay);
  }

  /**
   * Load wall time t afresh: with MSE a new stream connection; without, a
   * playback window around t.
   * `after`: when continuing past the end of a window, the wall time the new
   * window must get beyond; otherwise playback stops instead of looping.
   */
  async load(t, autoplay, after = null) {
    if (MSE) return this.stream(isLiveTime(t) ? null : t, autoplay, true);
    const seq = ++this.seq;
    const card = this.card;
    const now = nowS();
    t = Math.min(t, now - LIVE_LAG);
    this.target = t;
    this.autoplay = autoplay; // play() / pause() during the load change it
    this.lastLoad = { at: Date.now(), wall: t };
    if (card._master === this) card._paint(t);
    this.freeze();
    this.video.pause(); // the old window mustn't play (or move the playhead) under the veil
    this.setStatus("Loading", "loading", this.subline(t));
    this.loading = true;
    let res;
    try {
      res = await card._ws({
        type: "surveillance_station/vod",
        camera_id: this.cameraId,
        start: t - PRE_ROLL,
        end: Math.min(t + WINDOW_AHEAD, now),
      });
    } catch (e) {
      if (seq === this.seq) {
        this.loading = false;
        this.setStatus("Playback failed", "error", errText(e));
      }
      return;
    }
    if (seq !== this.seq) return;
    // The old media goes now: the new session's mapping must never be
    // applied to it. `loading` stays set until the new media is attached.
    this.destroyMedia();
    this.gap = false;
    const stop = (text) => {
      this.loading = false;
      this.session = null;
      this.setStatus(text, "empty", this.subline(t));
    };
    if (!res.url) return stop("No recording at this time");
    if (after != null && res.end <= after + 1) return stop("No later recording");
    // Requested start: time before the first run is a known gap, not "outside".
    res.reqStart = t - PRE_ROLL;
    this.session = res;
    if (card._master !== this && this.exactMedia(t) == null) this.autoplay = false;
    let start = this.wallToMedia(t);
    if (start == null && res.live) start = this.wallToMediaLive(t);
    if (start == null) return stop(`No recording after ${fmtTime(t)}`);
    if (t < res.runs[0].wall_start - 1) this.target = res.runs[0].wall_start; // started in a gap
    this.loading = false;
    const v = this.video;
    v.playbackRate = v.defaultPlaybackRate = card._rate;
    // No MSE (Safari before 17.1): the browser plays HLS itself.
    if (!v.canPlayType("application/vnd.apple.mpegurl")) {
      this.setStatus("This browser can't play this video", "error");
      return;
    }
    v.src = res.url;
    v.addEventListener(
      "loadedmetadata",
      () => {
        if (seq !== this.seq) return;
        v.currentTime = start;
        if (this.autoplay) v.play().catch(() => {});
      },
      { once: true }
    );
    v.addEventListener(
      "error",
      () => seq === this.seq && this.session === res && this.setStatus("Playback error", "error", this.subline(this.wall())),
      { once: true }
    );
    // A live playlist grows, and may gain a gap: keep the wall-clock mapping
    // up to date with it.
    if (res.live) {
      const token = res.url.split("/").at(-2);
      const timer = setInterval(async () => {
        if (this.session !== res) return clearInterval(timer);
        try {
          const upd = await card._ws({ type: "surveillance_station/vod_runs", token });
          if (this.session === res) Object.assign(res, upd);
        } catch (e) {
          /* the next round retries */
        }
      }, 10000);
    }
  }

  /**
   * Play SS's stream: the camera live (at == null) or its recordings from
   * wall time `at`. An open recordings stream jumps there, at once when it's
   * in what's buffered (unless `viaSS`: a follower whose stream is barely
   * ahead of the master needs SS to send from further on); `fresh` connects
   * anew instead.
   */
  async stream(at, autoplay, fresh = false, viaSS = false) {
    const card = this.card;
    const live = at == null;
    const isMaster = card._master === this;
    // A follower asks for a little more, then waits for the master (see FOLLOW_LEAD).
    const ask = live || isMaster ? at : at + FOLLOW_LEAD * this.lead * card._rate;
    const f = this.feed;
    if (!live && !fresh && f && !f.live && !f.ended && f.started) {
      this.seq++; // cancels a pending reconnect, or a live connection on its way
      this.goingLive = this.reconnecting = this.connecting = false;
      this.autoplay = autoplay;
      this.target = at;
      this.gap = false;
      if (this.veilKind && this.veilKind !== "loading") this.setStatus("");
      if (isMaster) card._paint(at);
      const m = this.loading || viaSS ? null : f.mediaAt(at);
      if (m != null) {
        this.asked = at;
        this.video.currentTime = m;
        // A follower is started by follow(), which knows whether the master plays.
        if (!autoplay) this.video.pause();
        else if (isMaster) this.video.play().catch(() => {});
        return;
      }
      this.asked = ask;
      this.lastLoad = { at: Date.now(), wall: at };
      this.video.pause();
      this.loading = true;
      this.veilSoon(this.subline(at));
      f.seekTo(ask);
      return;
    }
    const seq = ++this.seq;
    const name = card._cameraName(this.cameraId);
    this.target = live ? nowS() : at;
    this.asked = ask;
    this.autoplay = autoplay;
    this.lastLoad = { at: Date.now(), wall: this.target };
    if (isMaster) card._paint(this.target);
    this.freeze();
    this.video.pause();
    this.setStatus(live ? "Connecting" : "Loading", "loading", live ? `${name} · Live` : this.subline(at));
    this.loading = true;
    this.goingLive = live;
    this.reconnecting = false;
    this.connecting = true; // the current feed ending meanwhile changes nothing
    let res;
    try {
      res = await card._ws({ type: "surveillance_station/live", camera_id: this.cameraId, ...(live ? {} : { time: ask }) });
    } catch (e) {
      if (seq === this.seq) {
        this.loading = this.goingLive = this.connecting = false;
        this.setStatus(live ? "Live view failed" : "Playback failed", "error", errText(e), () => this.stream(at, true, true));
      }
      return;
    }
    if (seq !== this.seq) return;
    this.connecting = false;
    this.destroyMedia();
    this.gap = false;
    this.session = { live, ws: true };
    const url = card._hass.hassUrl(res.url).replace(/^http/, "ws");
    const feed = new StreamFeed(this, url, {
      at: ask,
      speed: card._rate,
      onStart: () => {
        if (feed !== this.feed) return;
        this.startedAt = Date.now();
        this.landed();
      },
      onLanded: () => feed === this.feed && this.landed(),
      onEnd: (why, detail) => feed === this.feed && this.streamEnded(feed, why, detail),
    });
    this.feed = feed;
    this.syncAudio();
  }

  /** The stream shows the place asked for (or the footage after it). */
  landed() {
    this.loading = this.goingLive = this.streamFailed = false;
    if (this.veilKind === "loading") this.setStatus("");
    clearTimeout(this.veilTimer);
    const card = this.card;
    if (card._master === this || this.feed.live) {
      if (this.autoplay) this.video.play().catch(() => {});
    } else this.justLanded = true; // follow() lines it up and starts it
    if (card._master !== this) return;
    // Nothing recorded at the time asked for: SS went on to the next
    // footage, and the others go there too, straight away.
    const t = this.wall();
    const skipped = !this.feed.live && t - this.target > 5;
    card._onTime();
    if (skipped) for (const p of card._players.values()) if (p !== this) p.seek(t, this.autoplay);
  }

  /** The stream ended: reconnect where it was, backing off, or give up. */
  streamEnded(feed, why, detail) {
    // A new connection is on its way: it replaces this one anyway.
    if (this.connecting) {
      feed.close();
      return;
    }
    const playing = this.intendsPlay();
    const live = feed.live;
    const at = live ? null : this.wall(); // what it showed, or where it was going
    const started = feed.started;
    this.destroyMedia();
    this.loading = this.goingLive = false;
    this.session = null;
    const name = this.card._cameraName(this.cameraId);
    const retry = () => {
      this.drops = 0;
      this.reconnecting = false;
      this.streamFailed = false;
      const m = this.card._master;
      this.stream(live ? null : m && m !== this ? m.wall() : at, true, true);
    };
    if (why === "codec") {
      this.streamFailed = true;
      if (/^(hev1|hvc1)/.test(detail ?? "")) this.card._noHevc();
      this.setStatus("This browser can't decode this camera's video", "error", detail ?? "", retry);
      return;
    }
    // Reconnect, backing off (1, 2, 4 s) while it keeps failing straight
    // away, or never starts (refused, SS unreachable).
    const quick = !started || Date.now() - this.startedAt < 10000;
    this.drops = quick ? (this.drops ?? 0) + 1 : 1;
    if (this.drops > 3) {
      this.streamFailed = true;
      this.setStatus(live ? "Live view unavailable" : "Playback unavailable", "error", name, retry);
      // And keep trying, slowly, while the card is on screen: a wall tablet
      // must come back by itself after the NAS reboots (monthly DSM updates).
      const seq = this.seq;
      const later = () => {
        if (seq !== this.seq || !this.streamFailed) return; // moved on, or retried by hand
        if (document.visibilityState === "visible" && this.card.isConnected) retry();
        else setTimeout(later, STREAM_RETRY_MS);
      };
      setTimeout(later, STREAM_RETRY_MS);
      return;
    }
    this.setStatus("Reconnecting", "loading", live ? `${name} · Live` : this.subline(at));
    const seq = this.seq;
    this.reconnecting = true; // follow() leaves it to this
    setTimeout(() => seq === this.seq && this.stream(at, playing, true), 1000 * 2 ** (this.drops - 1));
  }

  /** Sound: from the master, when not muted, live or at 1x. */
  syncAudio() {
    const f = this.feed;
    f?.setAudio(this.card._master === this && !this.video.muted && (f.live || this.card._rate === 1));
  }

  /** The card's playback speed changed. */
  setRate(rate) {
    if (this.feed) this.feed.setSpeed(rate);
    else this.video.playbackRate = this.video.defaultPlaybackRate = rate;
    this.syncAudio();
  }

  /** At the end of an HLS window, carry on into whatever was recorded next. */
  async continue() {
    // "ended" means the playlist is closed: an old window, or a live one that
    // hit the server's window cap.
    const s = this.session;
    if (!s || this.feed) return;
    const seq = this.seq;
    const next = s.end;
    this.setStatus("Finding the next recording", "loading", this.subline(next));
    let recs;
    try {
      recs = (
        await this.card._ws({
          type: "surveillance_station/recordings",
          camera_id: this.cameraId,
          start: Math.floor(next),
          end: Math.ceil(nowS()),
        })
      ).recordings;
    } catch (e) {
      if (seq === this.seq) this.setStatus("Playback failed", "error", errText(e));
      return;
    }
    // A seek inside the window while we were asking also cancels this.
    if (seq !== this.seq || !this.video.ended) return;
    const rec = recs.find((r) => (r.live ? nowS() : r.end) > next + 1);
    if (!rec) {
      this.setStatus("No later recording", "empty", this.subline(next));
      return;
    }
    this.load(Math.max(rec.start, next), true, next);
  }

  /**
   * Grid follower: stay at the master's wall time. Small drift is closed by
   * nudging the speed, large drift by seeking; a moment this camera has no
   * footage for pauses it behind a "No recording" veil.
   */
  follow(wall, playing, rate, stalled = false, masterLive = false) {
    if (this.loading) return;
    if (masterLive) {
      // The master is on the real-time stream: so is this camera, each
      // keeping its own margin; only play / pause is shared.
      if (!this.feed?.live) {
        const last = this.lastLoad;
        const wasLive = last && isLiveTime(last.wall + (Date.now() - last.at) / 1000);
        if (!this.streamFailed && !this.reconnecting && (!last || Date.now() - last.at > 5000 || !wasLive)) this.stream(null, playing);
        return;
      }
      const v = this.video;
      if (playing && v.paused) v.play().catch(() => {});
      else if (!playing && !v.paused) v.pause();
      return;
    }
    if (MSE) return this.followStream(wall, playing, rate, stalled);
    const s = this.session;
    const inWindow = s && wall >= Math.min(s.start, s.reqStart ?? s.start) - 1 && (s.live || wall < s.end);
    if (!inWindow) {
      const last = this.lastLoad;
      // Nothing recorded in the window we last asked for: don't ask again
      // until the master leaves it; otherwise at most every 5 s.
      const covered = !s && last && wall >= last.wall - PRE_ROLL && wall < last.wall + WINDOW_AHEAD;
      if (!covered && (!last || Date.now() - last.at > 5000)) this.load(wall, playing);
      return;
    }
    if (!this.mediaReady) return;
    const v = this.video;
    const target = this.exactMedia(wall);
    if (target == null) {
      if (!v.paused) v.pause();
      if (this.veilKind !== "empty") this.setStatus("No recording", "empty", this.subline(wall));
      this.gap = true;
      return;
    }
    if (this.gap) {
      this.gap = false;
      if (this.veilKind === "empty") this.setStatus("");
    }
    // Let a seek land before judging the drift again (slow segments would
    // otherwise be re-seeked forever).
    if (v.seeking) return;
    if (stalled) {
      if (!v.paused) v.pause();
      return;
    }
    const drift = v.currentTime - target;
    let r = rate;
    // At 8x, 2 s of drift is a quarter second: scale the seek threshold.
    if (Math.abs(drift) > Math.max(DRIFT_JUMP, 1.5 * rate) || (!playing && Math.abs(drift) > 0.15)) {
      v.currentTime = target;
    } else if (playing && Math.abs(drift) > DRIFT_NUDGE) r = rate * (1 - clamp(drift * 0.5, -0.2, 0.2));
    if (v.playbackRate !== r) v.playbackRate = r;
    // play() on an ended element restarts it from the top; the window's end
    // is handled by the reload above instead.
    if (playing && v.paused && !v.ended) v.play().catch(() => {});
    if (!playing && !v.paused) v.pause();
  }

  /**
   * follow() on SS's stream. Within what's buffered a follower lines up at
   * once; ahead of the master it holds its frame until the master gets
   * there (showing "No recording" when that's a gap in its recordings);
   * a little off, it plays a little faster or slower; further behind, it
   * jumps (SS never sends faster than the speed asked for).
   */
  followStream(wall, playing, rate, stalled) {
    const f = this.feed;
    if (!f || f.live) {
      const last = this.lastLoad;
      if (!this.streamFailed && !this.reconnecting && (!last || Date.now() - last.at > 3000)) this.stream(wall, playing);
      return;
    }
    if (!this.mediaReady || !f.started) return;
    const v = this.video;
    const drift = this.wall() - wall; // footage seconds; > 0: ahead of the master
    const lead = FOLLOW_LEAD * this.lead * rate;
    let landedBehind = false;
    if (this.justLanded) {
      // Landed behind (a keyframe further back than the lead): ask for more
      // (again, now: it doesn't loop, the lead doubles up to 8x). Landed
      // ahead: a little less next time, so each camera ends up with a lead
      // that fits how far apart its keyframes are.
      this.justLanded = false;
      landedBehind = drift < -0.1 * rate - 0.05;
      this.lead = landedBehind ? Math.min(this.lead * 2, 8) : Math.max(1, this.lead * 0.8);
      this.held = drift > 0;
    }
    const near = 0.6 * rate + 0.2; // this much off is made up by speed
    let nudge = 1;
    let hold = false;
    if (this.held && drift > 0.03) hold = true; // released once in step, not near it
    else if (Math.abs(drift) > 0.1 * rate + 0.05) {
      const m = Math.abs(drift) > near || !playing ? f.mediaAt(wall) : null;
      if (m != null) v.currentTime = m;
      else if (drift > 0) {
        // The master went back before what this camera asked for: go there too.
        if (wall < (this.asked ?? wall) - lead - 1) return this.resync(wall, playing);
        // Once holding (a jump landed ahead, as asked), until it's in step.
        if (drift > near || this.held) hold = true;
        else nudge = 1 - clamp((drift / rate) * 0.5, 0, 0.2);
      } else {
        // Speed only helps with footage for the master's time on its way
        // (SS paces its stream: it doesn't catch up by itself).
        const edge = (f.lastWall ?? 0) - wall;
        if (-drift > near || edge < 0.2 * rate) return this.resync(wall, playing, landedBehind);
        nudge = 1 + clamp((-drift / rate) * 0.5, 0, 0.2);
      }
    }
    f.nudge = nudge;
    this.held = hold;
    // Further ahead than the lead explains: nothing recorded here until then
    // (and still, until it plays again).
    const gap = hold && (this.gap || drift > lead + 2 * rate + 1);
    if (gap !== !!this.gap) {
      this.gap = gap;
      if (gap) this.setStatus("No recording", "empty", this.subline(wall));
      else if (this.veilKind === "empty") this.setStatus("");
    }
    if (v.seeking) return;
    const run = playing && !stalled && !hold;
    if (run && v.paused) v.play().catch(() => {});
    if (!run && !v.paused) v.pause();
  }

  /** Jump to the master's time, unless the last jump is still recent. */
  resync(wall, playing, now = false) {
    const last = this.lastLoad;
    if (!now && last && Date.now() - last.at < 1500) return;
    this.stream(wall, playing, false, true);
  }
}

class SSTimelineCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._cameras = [];
    this._cameraId = null; // the master's camera
    this._players = new Map(); // cameraId -> Player
    this._master = null;
    this._recs = [];
    this._bookmarks = [];
    this._rate = 1;
    this._tlSeq = 0;
    this._followPausedUntil = 0;
    this._shown = []; // camera ids on screen, in camera order
    this._grid = true; // grid mode: chips add / remove cameras; else they switch the one shown
    this._gridSet = []; // the grid's cameras, kept while one camera is shown
    this._evItems = []; // event list: bookmarks of the shown cameras, newest first, as loaded
    this._kinds = new Set(prefs.get("kinds", [])); // only these kinds listed and on the timeline ("Person"); none: all
    this._evKinds = []; // [kind, count]: what there is to choose from
    this._search = null; // smart search shown instead of the bookmarks: {params, label, items, loading, error}
    this._srSeq = 0;
    this._evSeq = 0;
    this._drag = false;
    this._onFullscreen = () => this._fullscreenChanged();
    this._evNodes = new Map(); // event id / day -> its element, kept across redraws
    // Keep the stage sized to the screen (see _fit). A mobile browser's
    // address bar sliding in and out changes the height by a toolbar's worth
    // while scrolling; that alone doesn't resize the video.
    this._onResize = () => {
      const { innerWidth: w, innerHeight: h } = window;
      if (w === this._vw && Math.abs(h - this._vh) < 120) return;
      this._fit();
    };
    this._resizeObs = new ResizeObserver(() => requestAnimationFrame(() => this._fit()));
    // A notification link opened while the card is already on screen: HA
    // navigates in place (no reload), so the card isn't set up again.
    // (A link is taken out of the address once followed, so going back or
    // closing a dialog later doesn't return to it.)
    this._onLocation = () => {
      if (this._inited && this._master && this.isConnected) this._followLink();
    };
    // HA came back (e.g. restarted): its playback sessions and thumbnail
    // links are gone, and events may have been missed meanwhile. HA's own
    // dashboards usually rebuild their cards on reconnect (this one is then
    // disconnected by the time the timer fires); a card that stays catches up.
    this._onReconnect = () => setTimeout(() => this._reconnected(), 1000);
    // The popovers (date/time, what's shown) close on Escape or a tap anywhere else.
    this._onDocDown = (e) => {
      const path = e.composedPath();
      for (const [box, wrap] of [[this._jumpBox, "jumpwrap"], [this._showsBox, "showwrap"]])
        if (box && !box.hidden && !path.some((n) => n.classList?.contains(wrap))) box.hidden = true;
    };
  }

  _reconnected() {
    if (!this.isConnected || !this._inited) return;
    if (!this._master) {
      // Still on "Can't list cameras": try again now that HA is back.
      if (!this._camsFailed) return; // or still loading them
      this._inited = false;
      this._init();
      return;
    }
    const t = this._master.wall();
    const at = this._wantsLive(t) ? nowS() : t;
    const playing = this._master.intendsPlay();
    this._loadTimeline();
    // New sessions: a seek could stay inside one the server no longer has.
    this._master.load(at, playing);
    for (const p of this._players.values()) if (p !== this._master) p.load(at, playing);
    this._resetEvents();
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { span: 3600, ...config };
    // The nearest of the spans offered.
    const span = Number(prefs.get("span", Number(this._config.span))) || 3600;
    this._span = SPANS.map(([v]) => v).reduce((a, b) => (Math.abs(b - span) < Math.abs(a - span) ? b : a));
    const end = nowS() + this._span * 0.05;
    this._view = { start: end - this._span, end };
    // What's laid over the video; each can be hidden (see SHOWS).
    this._shows = Object.fromEntries(SHOWS.map(([k]) => [k, prefs.get(k, this._config[k] !== false)]));
  }

  set hass(hass) {
    const first = !this._hass;
    if (hass.connection !== this._hass?.connection) {
      this._hass?.connection?.removeEventListener?.("ready", this._onReconnect);
      if (this.isConnected) hass.connection?.addEventListener?.("ready", this._onReconnect);
    }
    this._hass = hass;
    setTimeFormat(hass.locale);
    if (first && this.isConnected) this._init();
  }

  getCardSize() {
    return 10;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6 };
  }

  connectedCallback() {
    if (this._hass && !this._inited) this._init();
    else if (this._inited && this._master && this._followLink()) this._resume = null;
    else if (this._inited && this._resume && this._master) {
      // Back on the view: live again if it was live, else where it was.
      const { t, playing, live } = this._resume;
      this._resume = null;
      const at = live ? nowS() : t;
      this._centerOn(at);
      this._loadTimeline();
      this._seekAll(at, playing);
      this._refreshEvents();
    }
    this._refresh = setInterval(() => this._periodic(), REFRESH_MS);
    this._syncTimer = setInterval(() => this._sync(), SYNC_MS);
    document.addEventListener("fullscreenchange", this._onFullscreen);
    document.addEventListener("pointerdown", this._onDocDown);
    window.addEventListener("resize", this._onResize);
    window.addEventListener("location-changed", this._onLocation);
    this._resizeObs?.observe(this);
    this._hass?.connection?.addEventListener?.("ready", this._onReconnect);
  }

  disconnectedCallback() {
    clearInterval(this._refresh);
    clearInterval(this._syncTimer);
    document.removeEventListener("fullscreenchange", this._onFullscreen);
    clearTimeout(this._idleTimer);
    // Leaving the page ends fullscreen, maybe after our listener is gone.
    this._wasFs = false;
    this._stage?.classList.remove("idle");
    window.removeEventListener("resize", this._onResize);
    window.removeEventListener("location-changed", this._onLocation);
    document.removeEventListener("pointerdown", this._onDocDown);
    this._resizeObs?.disconnect();
    this._hass?.connection?.removeEventListener?.("ready", this._onReconnect);
    // Whatever state it was in (loading, a gap), pick up there on return.
    if (this._master) {
      const t = this._master.wall();
      this._resume = { t, playing: this._master.intendsPlay(), live: this._wantsLive(t) };
    }
    for (const p of this._players.values()) {
      p.seq++;
      p.loading = false;
      p.goingLive = p.connecting = p.reconnecting = false;
      p.destroyMedia();
      p.session = null;
    }
  }

  // ---- setup ------------------------------------------------------------

  async _init() {
    this._inited = true;
    this._camsFailed = false;
    this._render();
    let res;
    try {
      res = await this._ws({ type: "surveillance_station/cameras" });
    } catch (e) {
      this._stageMessage("Can't list cameras", "error", errText(e));
      this._camsFailed = true;
      return;
    }
    this._cameras = res.cameras.filter((c) => c.enabled);
    this._searchable = !!res.search;
    if (this._searchForm) this._searchForm.hidden = !this._searchable;
    this._syncTools();
    if (!this._cameras.length) {
      this._stageMessage("No enabled cameras in Surveillance Station", "empty");
      return;
    }
    this._stageMessage("");
    const params = new URLSearchParams(location.search);
    const view = this._initialView();
    this._gridSet = view.cameras;
    this._grid = view.grid && this._cameras.length > 1;
    // A camera asked for (e.g. by a notification link) is added to the grid,
    // or is the one shown; else the one watched last.
    const asked = this._findCamera(params.get("ss_camera") ?? this._config.camera);
    const last = this._findCamera(prefs.get("camera", null));
    const cam =
      asked ?? (last && (!this._grid || this._gridSet.includes(last.id)) ? last : this._findCamera(this._gridSet[0]));
    this._shown = this._grid ? [...this._gridSet] : [cam.id];
    const t = Number(params.get("ss_time"));
    consumeLink();
    this._renderCameras();
    this._cameraId = cam.id;
    this._buildPlayers();
    this._markCameras();
    this._resetEvents();
    // Opening the card plays: live, or the moment a link asked for.
    const start = t > 0 ? t : nowS();
    this._centerOn(start);
    if (!this.isConnected) {
      // Left the view while the cameras were loading: start on return.
      this._resume = { t: start, playing: true, live: !(t > 0) };
      return;
    }
    this._loadTimeline();
    this._seekAll(start, true);
  }

  _findCamera(want) {
    if (want == null || want === "") return null;
    const w = String(want).toLowerCase();
    return this._cameras.find((c) => String(c.id) === w || c.name.toLowerCase() === w) ?? null;
  }

  /**
   * The grid's cameras (the viewer's last pick, else the `cameras` option,
   * else all) and whether the grid is on (the viewer's, else `view`).
   */
  _initialView() {
    const known = (ids) => (Array.isArray(ids) ? ids.filter((id) => this._cameras.some((c) => c.id === id)) : []);
    let ids = known(prefs.get("cameras", null));
    let grid = prefs.get("grid", null);
    if (grid === null && ids.length === 1) {
      // Before 0.8.1 one camera shown was the single view, and the grid's
      // cameras were kept aside.
      grid = false;
      prefs.set("camera", ids[0]);
      ids = known(prefs.get("cameras_prev", null));
      prefs.set("grid", false);
      if (ids.length) prefs.set("cameras", ids);
    }
    if (!ids.length && Array.isArray(this._config.cameras)) {
      ids = this._config.cameras.map((x) => this._findCamera(x)?.id).filter((id) => id != null);
    }
    return {
      cameras: ids.length ? ids : this._cameras.slice(0, DEFAULT_GRID_MAX).map((c) => c.id),
      grid: grid ?? this._config.view !== "single",
    };
  }

  /** A camera turned out to be H.265 and this browser can't decode it: say why (once), not before. */
  _noHevc() {
    const warn = this.shadowRoot?.querySelector(".warn");
    if (warn && !warn.textContent && hevcSupport() === false) {
      warn.textContent = "This browser can't decode H.265 (HEVC), which these cameras record in. Chrome or Edge with hardware decoding, Safari, and the Home Assistant apps can.";
    }
  }

  _cameraName(id) {
    return this._cameras.find((c) => c.id === id)?.name ?? `Camera ${id}`;
  }

  _ws(msg) {
    if (this._config.entry_id) msg = { ...msg, entry_id: this._config.entry_id };
    return this._hass.callWS(msg);
  }

  _render() {
    const root = this.shadowRoot;
    const icon = (i) => `<ha-icon icon="${i}"></ha-icon>`;
    const skip = (d, i, cls = "") =>
      `<button class="icon ${cls}" data-skip="${d}" title="${d < 0 ? "Back" : "Forward"} ${Math.abs(d)} s">${icon(i)}</button>`;
    root.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="layout${prefs.get("events", true) ? "" : " noside"}">
          <div class="main">
            <div class="head">
              <div class="cams"></div>
              <button class="evtoggle" data-act="events" title="Events">${icon("mdi:bookmark-multiple-outline")}<span class="badge evcount"></span></button>
            </div>
            <div class="stagebox">
              <div class="stage single nocam">
                <div class="clock"></div>
                <div class="livetag" hidden>LIVE</div>
                <div class="stage-msg">${VEIL_HTML}</div>
                <div class="fsbar">
                  <button class="icon" data-act="play" title="Play / pause">${icon("mdi:play")}</button>
                  ${skip(-30, "mdi:rewind-30")}${skip(-10, "mdi:rewind-10")}${skip(10, "mdi:fast-forward-10")}${skip(30, "mdi:fast-forward-30")}
                  <span class="fsclock"></span>
                  <button class="icon" data-act="solo">${icon("mdi:view-grid-outline")}</button>
                  <button class="icon" data-act="fs" title="Exit fullscreen">${icon("mdi:fullscreen-exit")}</button>
                </div>
              </div>
            </div>
            <div class="warn"></div>
            <div class="controls">
              <button class="icon" data-act="play" title="Play / pause">${icon("mdi:play")}</button>
              ${skip(-30, "mdi:rewind-30", "wide")}${skip(-10, "mdi:rewind-10")}${skip(10, "mdi:fast-forward-10")}${skip(30, "mdi:fast-forward-30", "wide")}
              <select class="speed" title="Playback speed">
                ${SPEEDS.map((v) => `<option value="${v}">${v}×</option>`).join("")}
              </select>
              <button class="live" data-act="live" title="Watch live">${icon("mdi:access-point")}<span class="txt">Live</span></button>
              <span class="spacer"></span>
              <span class="jumpwrap">
                <button class="icon" data-act="jump" title="Go to a date and time">${icon("mdi:calendar-clock")}</button>
                <span class="jump" hidden>
                  <input type="datetime-local" step="1" class="when" />
                  <button data-act="go">Go</button>
                </span>
              </span>
              <button class="icon" data-act="solo">${icon("mdi:view-grid-outline")}</button>
              <span class="showwrap">
                <button class="icon" data-act="shows" title="What's shown on the video">${icon("mdi:eye-outline")}</button>
                <span class="jump shows" hidden>
                  ${SHOWS.map(([k, label]) => `<button data-show="${k}" aria-pressed="true">${icon("mdi:checkbox-marked")}<span>${label}</span></button>`).join("")}
                </span>
              </span>
              <button class="icon" data-act="mute" title="Sound">${icon("mdi:volume-off")}</button>
              <button class="icon" data-act="fs" title="Fullscreen">${icon("mdi:fullscreen")}</button>
            </div>
            <div class="tlbar">
              <button class="icon" data-act="pan-back" title="Earlier">${icon("mdi:chevron-left")}</button>
              <div class="range"></div>
              <div class="spans">${SPANS.map(([v, l]) => `<button data-span="${v}">${l}</button>`).join("")}</div>
              <button class="icon" data-act="pan-fwd" title="Later">${icon("mdi:chevron-right")}</button>
            </div>
            <div class="track">
              <div class="bars"></div>
              <div class="ph"></div>
              <div class="hover" hidden></div>
            </div>
          </div>
          <aside class="side">
            <div class="ev-head">
              <span class="ev-title">Events</span><span class="badge evtotal"></span>
              <span class="spacer"></span>
              <button class="icon" data-act="events" title="Hide events">${icon("mdi:close")}</button>
            </div>
            <div class="ev-tools">
              <form class="ev-search" hidden>${icon("mdi:magnify")}<input type="search" enterkeyhint="search" autocomplete="off"
                placeholder="Search: white car, person with a box…" aria-label="Smart search (Frigate)" /></form>
              <div class="ev-sq" hidden><span class="t"></span><button type="button" class="icon" data-act="search-close" title="Back to all events">${icon("mdi:close")}</button></div>
              <div class="ev-kinds" hidden></div>
            </div>
            <div class="ev-list"><div class="ev-items"></div><div class="ev-foot"></div></div>
          </aside>
        </div>
      </ha-card>`;

    const $ = (q) => root.querySelector(q);
    this._layoutEl = $(".layout");
    this._main = $(".main");
    this._stage = $(".stage");
    this._clock = $(".clock");
    this._liveTag = $(".livetag");
    this._fsClock = $(".fsclock");
    this._stageVeil = $(".stage-msg .veil");
    this._track = $(".track");
    this._bars = $(".bars");
    this._ph = $(".ph");
    this._hover = $(".hover");
    this._rangeEl = $(".range");
    this._range = null;
    this._evList = $(".ev-list");
    this._evItemsEl = $(".ev-items");
    this._evFoot = $(".ev-foot");
    this._searchForm = $(".ev-search");
    this._searchInput = $(".ev-search input");
    this._searchHead = $(".ev-sq");
    this._kindsEl = $(".ev-kinds");
    this._searchForm.hidden = !this._searchable;
    this._searchForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = this._searchInput.value.trim();
      if (q) this._runSearch({ query: q }, `“${q}”`);
      else this._endSearch();
      this._searchInput.blur(); // a phone's keyboard goes away, the results show
    });
    // The field's own clear (×): back to all events.
    this._searchInput.addEventListener("search", () => !this._searchInput.value && this._endSearch());
    this._drawKinds();
    this._syncTools();
    this._when = $(".when");
    this._jumpBox = $(".jump:not(.shows)");
    this._showsBox = $(".shows");


    root.querySelector("ha-card").addEventListener("click", (e) => this._onClick(e));
    $(".speed").addEventListener("change", (e) => {
      this._rate = Number(e.target.value);
      for (const p of this._players.values()) p.setRate(this._rate);
    });
    // Fullscreen controls reappear on any touch / mouse movement.
    this._stage.addEventListener("pointerdown", () => this._wakeFsBar());
    this._stage.addEventListener("pointermove", (e) => e.pointerType === "mouse" && this._wakeFsBar());

    // Timeline: drag to scrub, release to seek; a tap on a bookmark pin opens that event.
    const tr = this._track;
    tr.addEventListener("pointerdown", (e) => {
      tr.setPointerCapture(e.pointerId);
      this._drag = true;
      this._down = { x: e.clientX, bm: e.target.closest("[data-bm]")?.dataset.bm };
      this._showHover(e);
    });
    tr.addEventListener("pointermove", (e) => this._showHover(e));
    tr.addEventListener("pointerup", (e) => {
      if (!this._drag) return;
      this._drag = false;
      this._hover.hidden = true;
      const bm = Math.abs(e.clientX - this._down.x) < 6 && this._down.bm;
      const ev = bm && this._bookmarks.find((x) => String(x.id) === bm);
      if (ev) this._jumpToEvent(ev);
      else this._seekAll(liveIfRecent(this._timeAt(e)), true);
    });
    tr.addEventListener("pointercancel", () => {
      this._drag = false;
      this._hover.hidden = true;
    });
    tr.addEventListener("pointerenter", () => (this._overTrack = true));
    tr.addEventListener("pointerleave", () => {
      this._overTrack = false;
      if (!this._drag) this._hover.hidden = true;
    });

    // Events: thumbnails that fail (no recording any more) fall back to the icon;
    // the next page loads when the end of the list comes near.
    // A thumbnail that failed shows the icon, and is tried once more a little
    // later (the NAS may just have been busy); rows live on across refreshes.
    this._evList.addEventListener(
      "error",
      (e) => {
        const img = e.target;
        if (img.tagName !== "IMG") return;
        img.classList.add("bad");
        if (img.dataset.retried) return;
        img.dataset.retried = "1";
        setTimeout(() => img.isConnected && (img.src = img.src), THUMB_RETRY_MS);
      },
      true
    );
    this._evList.addEventListener("load", (e) => e.target.tagName === "IMG" && e.target.classList.remove("bad"), true);
    this._evObserver?.disconnect();
    this._evObserver = new IntersectionObserver(
      (entries) => entries.some((x) => x.isIntersecting) && this._loadMoreEvents(),
      { root: this._evList, rootMargin: "300px" }
    );
    this._evObserver.observe(this._evFoot);
    this._evNodes.clear(); // they belonged to the old list element
    for (const box of [this._jumpBox, this._showsBox])
      box.addEventListener("keydown", (e) => {
        if (e.key === "Escape") box.hidden = true;
      });

    this._markSpan();
    this._applyShows();
    this._resizeObs.observe(this);
  }

  _stageMessage(text, kind = "loading", sub = "") {
    setVeil(this._stageVeil, text, kind, sub);
  }

  _camColor(id) {
    const i = this._cameras.findIndex((c) => c.id === id);
    return CAM_COLORS[(i < 0 ? 0 : i) % CAM_COLORS.length];
  }

  _renderCameras() {
    this.shadowRoot.querySelector(".cams").innerHTML = this._cameras
      .map(
        (c) =>
          `<button data-cam="${c.id}" style="--cam:${this._camColor(c.id)}" title="Show / hide ${esc(c.name)}"><span class="dot"></span>${esc(c.name)}</button>`
      )
      .join("");
  }

  // ---- players / layout ---------------------------------------------------

  _makePlayer(id) {
    const p = new Player(this, id);
    p.el.style.setProperty("--cam", this._camColor(id));
    this._stage.insertBefore(p.el, this._clock);
    return p;
  }

  /**
   * Create / drop players to match this._shown; the master is this._cameraId.
   * One camera shown is the single view, several are the grid.
   */
  _buildPlayers() {
    // The master is always shown.
    if (!this._shown.includes(this._cameraId)) this._shown = [...this._shown, this._cameraId];
    const want = this._cameras.filter((c) => this._shown.includes(c.id)).map((c) => c.id);
    this._shown = want; // camera order
    for (const [id, p] of this._players) {
      if (!want.includes(id)) {
        p.destroy();
        this._players.delete(id);
      }
    }
    const added = [];
    for (const id of want) {
      if (!this._players.has(id)) {
        const p = this._makePlayer(id);
        this._players.set(id, p);
        added.push(p);
      }
    }
    // Keep camera order in the grid.
    for (const id of want) this._stage.insertBefore(this._players.get(id).el, this._clock);
    const n = want.length;
    const cols = n <= 1 ? 1 : n <= 4 ? 2 : 3;
    this._stage.style.setProperty("--cols", cols);
    this._stage.style.setProperty("--rows", Math.ceil(n / cols));
    this._stage.classList.toggle("grid", n > 1);
    this._stage.classList.toggle("single", n <= 1);
    this._stage.classList.toggle("nocam", n === 0);
    requestAnimationFrame(() => this._fit());
    this._setMaster(this._cameraId);
    for (const b of this.shadowRoot.querySelectorAll('[data-act="solo"]')) {
      b.hidden = this._cameras.length < 2;
      b.title = this._grid ? "One camera (the chips switch it)" : "Grid (the chips add and remove cameras)";
      b.querySelector("ha-icon").setAttribute("icon", this._grid ? "mdi:square-outline" : "mdi:view-grid-outline");
    }
    return added;
  }

  _setMaster(id) {
    const old = this._master;
    const p = this._players.get(id);
    if (!p) return;
    // A follower in a gap is paused on its next run; as master it holds the
    // time it was showing "No recording" for (play then goes to its next footage).
    if (p !== old && p.gap && old) p.target = old.wall();
    // Sound only from the master; it inherits the old master's choice (read
    // before the loop below mutes the old master).
    const muted = old && old !== p ? old.video.muted : p.video.muted;
    this._master = p;
    this._cameraId = id;
    prefs.set("camera", id);
    for (const q of this._players.values()) {
      q.el.classList.toggle("master", q === p);
      if (q !== p) q.video.muted = true;
    }
    p.video.muted = muted;
    for (const q of this._players.values()) q.syncAudio();
    // A follower holding its frame (waiting for the old master, or in a gap)
    // is paused: as master it carries on what the old one was doing.
    if (old && p !== old && old.intendsPlay() && !p.intendsPlay()) p.play();
    // A follower may have been mid-nudge; the master sets the pace.
    if (p.feed) p.feed.nudge = 1;
    else p.video.playbackRate = this._rate;
    this._markCameras();
    this._syncPlayIcon();
    this._syncMuteIcon();
  }

  /** Chips: tinted = shown, a ring in its colour = the master (when several are shown). */
  _markCameras() {
    this.shadowRoot.querySelector(".cams")?.classList.toggle("multi", this._shown.length > 1);
    for (const b of this.shadowRoot.querySelectorAll("[data-cam]")) {
      const id = Number(b.dataset.cam);
      b.classList.toggle("master", id === this._cameraId);
      b.classList.toggle("shown", this._shown.includes(id));
      b.setAttribute("aria-pressed", String(this._shown.includes(id)));
    }
  }

  /**
   * Show exactly these cameras (plus the master), keeping everyone at the
   * master's time. The timeline and the event list follow the shown set.
   */
  _setShown(ids, master = this._cameraId, at = null, autoplay = null, keep = true) {
    const m = this._master;
    if (!m) return;
    const wall = at ?? m.wall();
    const playing = autoplay ?? m.intendsPlay();
    const before = [...this._shown];
    this._shown = [...new Set([...ids, master])];
    this._cameraId = master;
    const added = this._buildPlayers();
    if (this._grid && keep) {
      this._gridSet = this._shown;
      prefs.set("cameras", this._shown);
    }
    for (const p of this._players.values()) p.zoom.reset(); // the cell size changed
    // New cells start empty. Moving a video element in the DOM pauses it:
    // the master resumes here, followers through follow().
    for (const p of added) p.load(wall, playing);
    const nm = this._master;
    if (!added.includes(nm) && !nm.loading && playing) nm.play();
    if (this._shown.join() !== before.join()) {
      this._recs = [];
      this._bookmarks = [];
      this._loadTimeline();
      this._resetEvents();
    }
  }

  /** Chip: in the grid, show / hide a camera (the last one stays); else show that one instead. */
  _chip(id) {
    if (this._grid) this._toggleCamera(id);
    else if (id !== this._cameraId) this._setShown([id], id);
  }

  _toggleCamera(id) {
    if (!this._shown.includes(id)) return this._setShown([...this._shown, id]);
    if (this._shown.length === 1) return;
    const rest = this._shown.filter((x) => x !== id);
    this._setShown(rest, id === this._cameraId ? rest[0] : this._cameraId);
  }

  /** Grid <-> one camera (the master; back in the grid, the grid's cameras). */
  _toggleGrid() {
    this._grid = !this._grid;
    prefs.set("grid", this._grid);
    if (!this._grid) return this._setShown([this._cameraId]);
    const set = this._gridSet.filter((id) => this._cameras.some((c) => c.id === id));
    const ids = set.length ? set : this._cameras.map((c) => c.id);
    this._setShown(ids, ids.includes(this._cameraId) ? this._cameraId : ids[0]);
  }

  /** Make a camera the master (showing it if needed) and go to t. */
  _selectCamera(id, t, autoplay, keep = true) {
    if (this._shown.includes(id)) this._setMaster(id);
    // _seekAll then finds it loading there.
    else this._setShown(this._grid ? [...this._shown, id] : [id], id, t, autoplay, keep);
    this._centerOn(t);
    this._loadTimeline();
    this._seekAll(t, autoplay);
  }

  // ?ss_camera=&ss_time= in the address: that camera at that moment (live
  // without a time), then out of the address. Another tap on the same
  // notification puts it back, and is followed again.
  _followLink() {
    const params = new URLSearchParams(location.search);
    if (!params.has("ss_time") && !params.has("ss_camera")) return false;
    consumeLink();
    const cam = this._findCamera(params.get("ss_camera")) ?? this._findCamera(this._cameraId);
    const t = Number(params.get("ss_time"));
    if (!cam) return false;
    this._followPausedUntil = 0;
    // Into the grid for now, not into the grid saved for next time.
    this._selectCamera(cam.id, t > 0 ? t : nowS(), true, false);
    this._refreshEvents();
    return true;
  }

  _seekAll(t, autoplay) {
    // Master first: followers then correct to wherever it actually lands.
    this._master?.seek(t, autoplay);
    for (const p of this._players.values()) if (p !== this._master) p.seek(t, autoplay);
  }

  _sync() {
    const m = this._master;
    if (this._players.size < 2 || !m) return;
    const followers = [...this._players.values()].filter((p) => p !== m);
    if (m.feed?.live || m.goingLive) {
      // Live: no waiting for the master's stalls (each stream has its own).
      for (const p of followers) p.follow(m.wall(), m.intendsPlay(), this._rate, false, true);
      return;
    }
    if (m.loading || (m.session && !m.mediaReady && !m.gap)) {
      // Wait where they are; they'll be moved once the master's media is up.
      for (const p of followers) if (!p.loading && !p.video.paused) p.video.pause();
      return;
    }
    // A master with nothing recorded at its time (no session, or in a gap)
    // still holds that time: the others show it, paused.
    const live = m.session && m.mediaReady && !m.gap;
    const wall = live ? m.wall() : m.target;
    const v = m.video;
    const playing = live && !v.paused;
    // A buffering master isn't moving: followers pause, but aren't re-seeked.
    const stalled = playing && (v.seeking || v.readyState < 3);
    for (const p of followers) p.follow(wall, playing && !stalled, this._rate, stalled);
  }

  _onCellTap(player, double, x, y) {
    if (this._players.size > 1) {
      if (double) {
        // Double tap a cell: that camera alone.
        this._setMaster(player.cameraId);
        this._toggleGrid();
      } else this._setMaster(player.cameraId);
    } else if (double) {
      player.zoom.toggleAt(x, y);
    }
  }

  _syncPlayIcon() {
    const paused = !this._master?.intendsPlay();
    for (const i of this.shadowRoot.querySelectorAll('[data-act="play"] ha-icon')) {
      i.setAttribute("icon", paused ? "mdi:play" : "mdi:pause");
    }
  }

  _syncMuteIcon() {
    const muted = !this._master || this._master.video.muted;
    const unplayable = this._master?.audioUnplayable;
    const button = this.shadowRoot.querySelector('[data-act="mute"]');
    button?.querySelector("ha-icon")?.setAttribute(
      "icon", unplayable && !muted ? "mdi:volume-variant-off" : muted ? "mdi:volume-off" : "mdi:volume-high"
    );
    button?.setAttribute("title", unplayable ? `Sound: this browser can't play this camera's audio (${unplayable})` : "Sound");
  }

  _applyShows() {
    const cls = { clock: "noclock", live_badge: "nolive", camera_names: "nonames" };
    for (const [k] of SHOWS) {
      this._stage.classList.toggle(cls[k], !this._shows[k]);
      const b = this.shadowRoot.querySelector(`[data-show="${k}"]`);
      b?.classList.toggle("on", this._shows[k]);
      b?.setAttribute("aria-pressed", String(this._shows[k]));
      b?.querySelector("ha-icon").setAttribute("icon", this._shows[k] ? "mdi:checkbox-marked" : "mdi:checkbox-blank-outline");
    }
    const all = SHOWS.every(([k]) => this._shows[k]);
    this.shadowRoot.querySelector('[data-act="shows"] ha-icon')?.setAttribute("icon", all ? "mdi:eye-outline" : "mdi:eye-off-outline");
  }

  // ---- fullscreen -----------------------------------------------------------

  async _toggleFullscreen() {
    const st = this._stage;
    if (this.shadowRoot.fullscreenElement || document.fullscreenElement) {
      await document.exitFullscreen?.().catch(() => {});
      return;
    }
    if (st.requestFullscreen) {
      try {
        await st.requestFullscreen({ navigationUI: "hide" });
      } catch (e) {
        // The host (e.g. a WebView without fullscreen support) said no: the
        // video's own fullscreen is the next best thing.
        this._videoFullscreen();
        return;
      }
      // Landscape where the platform allows it (not all WebViews do).
      screen.orientation?.lock?.("landscape").catch(() => {});
    } else if (st.webkitRequestFullscreen) {
      st.webkitRequestFullscreen();
    } else {
      this._videoFullscreen(); // iPhone: native player only
    }
  }

  _videoFullscreen() {
    const v = this._master?.video;
    try {
      // Throws before metadata; there's nothing to show fullscreen yet anyway.
      if (v && v.readyState >= 1) v.webkitEnterFullscreen?.();
    } catch (e) {
      /* stay inline */
    }
  }

  _fullscreenChanged() {
    const fs = this.shadowRoot.fullscreenElement === this._stage;
    if (fs === !!this._wasFs) return; // another element's fullscreen
    this._wasFs = fs;
    if (fs) this._wakeFsBar();
    else {
      screen.orientation?.unlock?.();
      clearTimeout(this._idleTimer);
      this._stage.classList.remove("idle");
    }
    // Zoom offsets were computed for the old size.
    for (const p of this._players.values()) p.zoom.reset();
    if (!fs) this._fit();
  }

  _wakeFsBar() {
    this._stage.classList.remove("idle");
    clearTimeout(this._idleTimer);
    this._idleTimer = setTimeout(() => this._stage.classList.add("idle"), FS_IDLE_MS);
  }

  // ---- controls -------------------------------------------------------------

  _onClick(e) {
    const b = e.target.closest("button");
    if (!b) return;
    const m = this._master;
    if (b.dataset.cam) {
      const id = Number(b.dataset.cam);
      this._chip(id);
      return;
    }
    if (b.dataset.act === "events") {
      const open = this._layoutEl.classList.toggle("noside") === false;
      prefs.set("events", open);
      this._fit();
      return;
    }
    // What's shown on the video: CSS only, no player needed.
    if (b.dataset.show) {
      const k = b.dataset.show;
      this._shows[k] = !this._shows[k];
      prefs.set(k, this._shows[k]);
      this._applyShows();
      return;
    }
    if (b.dataset.kind) {
      const k = b.dataset.kind;
      if (this._kinds.has(k)) this._kinds.delete(k);
      else this._kinds.add(k);
      prefs.set("kinds", [...this._kinds]);
      this._drawKinds();
      this._drawTimeline();
      this._resetEvents(); // the bookmarks, of these kinds (and a search shown, asked anew)
      return;
    }
    if (b.dataset.similar) {
      const id = Number(b.dataset.similar);
      const ev = this._evItems.find((x) => x.id === id) ?? this._search?.items.find((x) => x.bookmark_id === id);
      const what = ev ? `${ev.name} · ${this._cameraName(ev.camera_id)} ${fmtDate(ev.start)} ${fmtTime(ev.start)}` : "this event";
      this._runSearch({ bookmark_id: id }, `Similar to ${what}`);
      return;
    }
    if (b.dataset.act === "search-close") {
      this._endSearch();
      return;
    }
    if (b.dataset.act === "shows") {
      this._jumpBox.hidden = true;
      this._showsBox.hidden = !this._showsBox.hidden;
      // Into the list, so Escape (and the keyboard) work there straight away.
      if (!this._showsBox.hidden) this._showsBox.querySelector("button")?.focus();
      return;
    }
    if (!m) {
      // The camera list failed: try again.
      if (b.dataset.act === "retry") {
        this._inited = false;
        this._init();
      }
      return;
    }
    if (b.dataset.skip) {
      const d = Number(b.dataset.skip);
      // Forward past the newest recording: live.
      const t = d > 0 ? liveIfRecent(m.wall() + d) : m.wall() + d;
      this._seekAll(t, m.intendsPlay());
      return;
    }
    if (b.dataset.span) {
      this._span = Number(b.dataset.span);
      prefs.set("span", this._span);
      this._centerOn(m.wall());
      this._markSpan();
      this._loadTimeline();
      return;
    }
    if (b.dataset.sr) {
      const r = this._search?.items.find((x) => x.key === b.dataset.sr);
      if (r) this._jumpToEvent(r);
      return;
    }
    if (b.dataset.ev) {
      const ev = this._evItems.find((x) => String(x.id) === b.dataset.ev);
      if (ev) this._jumpToEvent(ev);
      return;
    }
    switch (b.dataset.act) {
      case "play":
        // Followers with media resume through follow(), which keeps a
        // follower in a gap paused.
        if (!m.intendsPlay()) {
          m.play();
          // L3: a follower with nothing loaded starts at the master's time.
          for (const p of this._players.values()) if (p !== m && !p.session && !p.loading) p.load(m.wall(), true);
        } else for (const p of this._players.values()) p.pause();
        break;
      case "live": {
        const t = nowS();
        this._centerOn(t);
        this._loadTimeline();
        this._seekAll(t, true);
        break;
      }
      case "jump":
        this._showsBox.hidden = true;
        this._jumpBox.hidden = !this._jumpBox.hidden;
        if (!this._jumpBox.hidden) {
          this._when.value = toLocalInput(m.wall());
          this._when.focus();
        }
        break;
      case "go": {
        const t = new Date(this._when.value).getTime() / 1000;
        if (!Number.isFinite(t)) return;
        this._jumpBox.hidden = true;
        this._centerOn(t);
        this._loadTimeline();
        this._seekAll(t, true);
        break;
      }
      case "retry": {
        const p = this._players.get(Number(b.closest(".cell")?.dataset.cell));
        const at = p && p !== this._master ? this._master.wall() : p?.target;
        if (p) (p.retry ?? (() => p.load(at, true)))();
        break;
      }
      case "mute":
        m.video.muted = !m.video.muted;
        m.syncAudio();
        this._syncMuteIcon();
        break;
      case "solo":
        this._toggleGrid();
        break;
      case "fs":
        this._toggleFullscreen();
        break;
      case "pan-back":
      case "pan-fwd": {
        const d = (b.dataset.act === "pan-back" ? -0.5 : 0.5) * this._span;
        const end = Math.min(this._view.end + d, nowS() + this._span * 0.05);
        this._view = { start: end - this._span, end };
        this._followPausedUntil = Date.now() + FOLLOW_PAUSE_MS;
        this._loadTimeline();
        break;
      }
    }
  }

  _markSpan() {
    for (const b of this.shadowRoot.querySelectorAll("[data-span]")) {
      b.classList.toggle("on", Number(b.dataset.span) === this._span);
    }
  }

  // ---- timeline ---------------------------------------------------------

  _centerOn(t) {
    const end = Math.min(t + this._span / 2, nowS() + this._span * 0.05);
    this._view = { start: end - this._span, end };
  }

  async _loadTimeline() {
    const seq = ++this._tlSeq;
    this._tlAt = Date.now();
    const { start, end } = this._view;
    this._drawTimeline();
    if (!this._shown.length) return;
    const shown = [...this._shown];
    const q = { start: Math.floor(start), end: Math.ceil(end) };
    this._tlBusy = true;
    try {
      // Recordings per camera shown; bookmarks from the same (cached) list
      // the event list pages through, so the two always agree.
      const [b, ...rs] = await Promise.all([
        this._ws({ type: "surveillance_station/bookmarks", camera_ids: shown, ...q }),
        ...shown.map((id) => this._ws({ type: "surveillance_station/recordings", camera_id: id, ...q })),
      ]);
      if (seq !== this._tlSeq) return;
      this._recs = rs.flatMap((r) => r.recordings);
      this._bookmarks = b.bookmarks;
    } catch (e) {
      // Keep the video usable; say it where the timeline is.
      if (seq === this._tlSeq) {
        this._rangeEl.textContent = `Timeline failed: ${errText(e)}`;
        this._range = null; // put the range back once it works again
      }
      return;
    } finally {
      if (seq === this._tlSeq) this._tlBusy = false;
    }
    this._drawTimeline();
  }

  _drawTimeline() {
    if (!this._bars) return;
    const { start, end } = this._view;
    const span = end - start;
    const now = nowS();
    const x = (t) => ((t - start) / span) * 100;
    let html = "";

    // Ticks as dense as their labels allow: ~48 px for a time, ~64 px for a date.
    const px = Math.max(this._track.clientWidth, 1);
    const step =
      TICK_STEPS.find((v) => (v * px) / span >= (v >= 86400 ? 64 : 48)) ?? TICK_STEPS[TICK_STEPS.length - 1];
    for (const t of ticksOf(start, end, step)) {
      const midnight = new Date(t * 1000).getHours() === 0;
      const label = step >= 86400 || (step >= 3600 && midnight) ? fmtDay(t) : fmtTime(t, false);
      // A label that would run off the right edge keeps just its tick.
      const room = x(t) < 100 - (label.length * 6 * 100) / Math.max(this._track.clientWidth, 1);
      html += `<div class="tick" style="left:${x(t)}%">${room ? `<span>${label}</span>` : ""}</div>`;
    }
    // With several cameras: time where any of them recorded.
    for (const [s, e] of unionOf(this._recs.map((r) => [r.start, r.live ? Math.max(r.end, Math.min(now, r.end + LIVE_GROW_MAX)) : r.end]))) {
      if (e < start || s > end) continue;
      const a = Math.max(x(s), 0);
      const b = Math.min(x(e), 100);
      html += `<div class="rec" style="left:${a}%;width:${Math.max(b - a, 0.1)}%"></div>`;
    }
    let prev = -Infinity;
    let dense = false;
    for (const bm of this._bookmarks) {
      if (bm.end < start || bm.start > end || !this._kindOk(bm.name)) continue;
      const at = (x(bm.start) * px) / 100;
      if (at - prev < 20) dense = true;
      prev = at;
      const a = Math.max(x(bm.start), 0);
      const b = Math.min(x(Math.max(bm.end, bm.start)), 100);
      const tip = `${fmtTime(bm.start)} ${this._cameraName(bm.camera_id)}: ${bm.name}${bm.comment ? " — " + bm.comment : ""}`;
      html += `<div class="bm" data-bm="${bm.id}" style="left:${a}%;width:${Math.max(b - a, 0)}%;--cam:${this._camColor(bm.camera_id)}" title="${esc(tip)}"></div>`;
    }
    if (now >= start && now <= end) html += `<div class="nowm" style="left:${x(now)}%" title="Now"></div>`;
    this._bars.innerHTML = html;
    this._track.classList.toggle("dense", dense);

    // Narrow cards get the dates only, as compact as possible ("Sep 18–25").
    const a = new Date(start * 1000);
    const z = new Date(end * 1000);
    const short =
      fmtDay(start) === fmtDay(end)
        ? fmtDay(start)
        : a.getMonth() === z.getMonth()
          ? `${fmtDay(start)}–${z.getDate()}`
          : `${fmtDay(start)}–${fmtDay(end)}`;
    const range = `<span class="long">${fmtDay(start)} ${fmtTime(start, false)} – ${
      fmtDay(end) === fmtDay(start) ? "" : fmtDay(end) + " "
    }${fmtTime(end, false)}</span><span class="short">${short}</span>`;
    if (range !== this._range) this._rangeEl.innerHTML = this._range = range;
    this._paint(this._currentWall());
  }

  _currentWall() {
    return this._master ? this._master.wall() : nowS() - LIVE_LAG;
  }

  // ---- event list -------------------------------------------------------

  /** Start the list over (the cameras shown changed, or first load). */
  _resetEvents() {
    // A search follows the cameras shown (not a reconnect or a periodic start-over).
    const s = this._search;
    if (s && (s.cams !== this._shown.join() || s.kinds !== this._kindsKey())) {
      // Asked once the chips settle: each search is a CLIP run on Frigate, one at a time.
      clearTimeout(this._srTimer);
      this._srTimer = setTimeout(() => this._search === s && this._runSearch(s.params, s.label), 300);
    } else clearTimeout(this._srTimer); // a chip turned back: as asked
    this._evSeq++;
    this._evItems = [];
    this._evMore = true;
    this._evLoading = false;
    this._evError = null;
    this._evTotal = null;
    // Thumbnail links are valid for about a day; the list is reloaded before.
    this._evLoadedAt = Date.now();
    this._drawEvents();
    this._loadMoreEvents();
  }

  /** The next page, older than the last event listed. */
  async _loadMoreEvents() {
    if (this._evLoading || !this._evMore || !this._shown.length || this._search) return;
    const seq = this._evSeq;
    const last = this._evItems.at(-1);
    this._evLoading = true;
    this._evError = null;
    this._drawFoot();
    let res;
    try {
      res = await this._ws({
        type: "surveillance_station/bookmark_page",
        camera_ids: this._shown,
        limit: EVENT_PAGE,
        ...this._kindsParam(),
        ...(last ? { before: last.start, before_id: last.id } : {}),
      });
    } catch (e) {
      if (seq === this._evSeq) {
        this._evLoading = false;
        this._evError = errText(e); // retried by _periodic
        this._drawFoot();
      }
      return;
    }
    if (seq !== this._evSeq) return;
    this._evLoading = false;
    const known = new Set(this._evItems.map((x) => x.id));
    this._evItems.push(...res.bookmarks.filter((x) => !known.has(x.id)));
    this._evMore = res.more;
    this._evTotal = res.total;
    this._setKinds(res.kinds);
    this._drawEvents();
    this._fillRoom();
  }

  /** Still room on screen (a tall sidebar): the next page. */
  _fillRoom() {
    requestAnimationFrame(() => {
      const r = this._evList.getBoundingClientRect();
      const f = this._evFoot.getBoundingClientRect();
      if (this._evMore && r.height && f.top < r.bottom + 300) this._loadMoreEvents();
    });
  }

  /**
   * Periodic: bring the loaded part of the list up to date with the newest
   * page. Events are ordered by start, and one can be bookmarked after a
   * later one (a detection that ended late), so new events are merged in by
   * time, not just put on top; events deleted in SS go away.
   */
  async _refreshEvents() {
    if (this._evLoading || !this._shown.length) return;
    if (this._evError || Date.now() - this._evLoadedAt > EVENT_LIST_MAX_AGE_MS) {
      if (this._evItems.length && !this._evError) this._resetEvents();
      else this._loadMoreEvents(); // the page that failed, again
      return;
    }
    const seq = this._evSeq;
    let res;
    try {
      res = await this._ws({
        type: "surveillance_station/bookmark_page", camera_ids: this._shown, limit: EVENT_PAGE, ...this._kindsParam(),
      });
    } catch (e) {
      return;
    }
    if (seq !== this._evSeq || this._evLoading) return;
    this._setKinds(res.kinds);
    const items = this._evItems;
    const page = res.bookmarks;
    // Newest first, as the server orders them.
    const cmp = (a, b) => b.start - a.start || b.id - a.id;
    if (res.more && items.length && cmp(page.at(-1), items[0]) < 0) {
      // More new events than one page (the view was away for a while): the
      // ones between the page and the list would be unreachable.
      this._resetEvents();
      return;
    }
    const inPage = new Map(page.map((x) => [x.id, x]));
    // What the page covers: everything down to its oldest (or all, if it's all there is).
    const covered = (x) => !res.more || cmp(x, page.at(-1)) <= 0;
    // Events still there take their current version (renamed / edited in SS).
    const kept = items.filter((x) => inPage.has(x.id) || !covered(x)).map((x) => inPage.get(x.id) ?? x);
    const edited = kept.some((x, i) => x !== items[i] && x.name + x.comment + x.end !== items[i].name + items[i].comment + items[i].end);
    const known = new Set(kept.map((x) => x.id));
    const oldest = items.at(-1);
    const added = page.filter((x) => !known.has(x.id) && (!oldest || !this._evMore || cmp(x, oldest) <= 0));
    const totalChanged = res.total !== this._evTotal;
    this._evTotal = res.total;
    if (!items.length) this._evMore = res.more;
    if (!added.length && kept.length === items.length && !edited) {
      // "Today" / "Yesterday" move on at midnight.
      if (totalChanged || this._evDrawnDay !== new Date().toDateString()) this._drawEvents();
      return;
    }
    this._evItems = [...kept, ...added].sort(cmp);
    this._drawEvents();
  }

  _drawFoot() {
    if (!this._evFoot) return;
    const s = this._search;
    if (s) {
      const n = this._searchShown().length;
      this._evFoot.textContent = s.loading
        ? "Searching…"
        : s.error
          ? `Search failed: ${s.error}`
          : n
            ? "Found by Frigate; plays from Surveillance Station"
            : s.items.length
              ? `No ${[...this._kinds].join(" / ")} matches`
              : "No matches";
      return;
    }
    const kinds = this._kinds.size ? `${[...this._kinds].join(" / ")} ` : "";
    this._evFoot.textContent = this._evError
      ? `Couldn't load events: ${this._evError}`
      : this._evLoading
        ? "Loading…"
        : this._evItems.length
          ? this._evMore ? "" : "No earlier events"
          : `No ${kinds}bookmarks for ${this._shown.length === 1 ? this._cameraName(this._shown[0]) : "these cameras"}`;
  }

  /**
   * Show _evItems. Rows are kept across redraws and only moved, added or
   * removed: a refresh doesn't reload thumbnails (or retry failed ones),
   * and doesn't disturb focus, hover or a tap in progress.
   */
  _drawEvents() {
    if (!this._evItemsEl) return;
    const today = new Date().toDateString();
    this._evDrawnDay = today;
    const yesterday = new Date(Date.now() - 86400000).toDateString();
    const want = [];
    const keys = new Set();
    let day = null;
    const node = (key, html) => {
      keys.add(key);
      let el = this._evNodes.get(key);
      if (!el) {
        const tpl = document.createElement("template");
        tpl.innerHTML = html.trim();
        el = tpl.content.firstElementChild;
        this._evNodes.set(key, el);
      }
      want.push(el);
    };
    const s = this._search;
    // "Similar" is offered on Frigate's bookmarks when its search can be asked.
    const similar = (id, comment) =>
      this._searchable && id != null && FRIGATE_REF.test(comment ?? "")
        ? `<button class="icon sim" data-similar="${id}" title="Find similar">${`<ha-icon icon="mdi:image-search-outline"></ha-icon>`}</button>`
        : "";
    for (const e of s ? this._searchShown() : this._evItems) {
      const d = new Date(e.start * 1000).toDateString();
      // Search results come best first: each says its day instead.
      if (!s && d !== day) {
        day = d;
        const label = d === today ? "Today" : d === yesterday ? "Yesterday" : fmtDate(e.start);
        node(`d:${d}:${label}`, `<div class="ev-day">${label}</div>`);
      }
      const dur = e.end > e.start ? fmtDur(e.end - e.start) : "";
      const thumb = e.thumbnail ? `<img loading="lazy" decoding="async" alt="" src="${esc(e.thumbnail)}">` : "";
      const when = s ? `${d === today ? "Today" : d === yesterday ? "Yesterday" : fmtDate(e.start)} ${fmtTime(e.start)}` : fmtTime(e.start);
      const id = s ? e.bookmark_id : e.id;
      node(
        `${s ? "s:" + e.key : "e:" + e.id}:${e.start}:${e.end}:${e.camera_id}:${e.name}:${e.comment}:${this._searchable}`,
        `<div class="evrow${similar(id, e.comment) ? " has-sim" : ""}"><button class="ev" ${s ? `data-sr="${esc(e.key)}"` : `data-ev="${e.id}"`} style="--cam:${this._camColor(e.camera_id)}">
          <span class="thumb"><ha-icon icon="mdi:cctv"></ha-icon>${thumb}${dur ? `<span class="dur">${dur}</span>` : ""}</span>
          <span class="evt">
            <span class="n">${esc(e.name || "(unnamed)")}</span>
            <span class="m"><i></i>${esc(this._cameraName(e.camera_id))} · ${when}</span>
            ${e.comment ? `<span class="c">${esc(e.comment)}</span>` : ""}
          </span>
        </button>${similar(id, e.comment)}</div>`
      );
    }
    for (const key of [...this._evNodes.keys()]) if (!keys.has(key)) this._evNodes.delete(key);
    const box = this._evItemsEl;
    let cur = box.firstChild;
    for (const el of want) {
      if (el === cur) cur = cur.nextSibling;
      else box.insertBefore(el, cur);
    }
    while (cur) {
      const next = cur.nextSibling;
      cur.remove();
      cur = next;
    }
    const total = s ? (s.loading ? "" : this._searchShown().length) : (this._evTotal ?? "");
    for (const q of [".evcount", ".evtotal"]) {
      const el = this.shadowRoot.querySelector(q);
      if (el) {
        el.textContent = total;
        el.hidden = total === "";
      }
    }
    this._drawFoot();
    this._activeEvent = undefined;
    this._markActiveEvent(this._currentWall());
    this._markScrolls();
  }

  /** Whether the list has more than it shows (see .ev-list.scrolls). */
  _markScrolls() {
    const l = this._evList;
    if (l) l.classList.toggle("scrolls", l.scrollHeight > l.clientHeight + 1);
  }

  // ---- kinds and smart search ----------------------------------------------

  _kindsParam() {
    return this._kinds.size ? { kinds: [...this._kinds] } : {};
  }

  _kindsKey() {
    return [...this._kinds].map((k) => k.toLowerCase()).sort().join();
  }

  /** Whether a bookmark (by its name, "Person, Car") or a result (its kind) is of a kind chosen. */
  _kindOk(name) {
    if (!this._kinds.size) return true;
    const want = new Set([...this._kinds].map((k) => k.toLowerCase()));
    return String(name ?? "").split(",").some((k) => want.has(k.trim().toLowerCase()));
  }

  _setKinds(kinds) {
    if (!Array.isArray(kinds)) return;
    const key = JSON.stringify(kinds);
    if (key === this._evKindsKey) return;
    this._evKindsKey = key;
    this._evKinds = kinds;
    // A kind chosen as another spelling ("car" once, "Car" now): as it is written now.
    const as = new Map(kinds.map(([k]) => [k.toLowerCase(), k]));
    const chosen = new Set([...this._kinds].map((k) => as.get(k.toLowerCase()) ?? k));
    if (chosen.size !== this._kinds.size || [...chosen].some((k) => !this._kinds.has(k))) {
      this._kinds = chosen;
      prefs.set("kinds", [...chosen]);
    }
    this._drawKinds();
  }

  /** The kind chips: what the bookmarks have, and whatever is chosen (even if none are left). */
  _drawKinds() {
    const el = this._kindsEl;
    if (!el) return;
    const counts = new Map(this._evKinds);
    for (const k of this._kinds) if (!counts.has(k)) counts.set(k, 0);
    el.hidden = counts.size < 2 && !this._kinds.size; // one kind only: nothing to choose
    this._syncTools();
    el.innerHTML = [...counts]
      .map(([k, n]) => {
        const on = this._kinds.has(k);
        return `<button type="button" data-kind="${esc(k)}" aria-pressed="${on}" class="${on ? "on" : ""}">${esc(k)}${
          n ? ` <span class="n">${n}</span>` : ""
        }</button>`;
      })
      .join("");
  }

  /** The tools row takes no room when it has nothing to show. */
  _syncTools() {
    const tools = this._searchForm?.parentElement;
    if (tools) tools.hidden = [...tools.children].every((c) => c.hidden);
  }

  _searchShown() {
    // As the list does, by the bookmark's name ("Person, Car" found by its car is a Person too).
    return (this._search?.items ?? []).filter((r) => this._kindOk(r.name));
  }

  /** Ask Frigate (through the integration): {query} or {bookmark_id} (similar to it). */
  async _runSearch(params, label) {
    const seq = ++this._srSeq;
    this._search = {
      params, label, items: [], loading: true, error: null, cams: this._shown.join(), kinds: this._kindsKey(),
    };
    this._searchHead.hidden = false;
    this._syncTools();
    this._searchHead.querySelector(".t").textContent = label;
    if (!("query" in params)) this._searchInput.value = "";
    this._evList.scrollTop = 0;
    this._drawEvents();
    let res;
    try {
      res = await this._ws({
        type: "surveillance_station/search", ...params, camera_ids: this._shown, ...this._kindsParam(), limit: 30,
      });
    } catch (e) {
      if (seq !== this._srSeq) return;
      this._search.loading = false;
      this._search.error = errText(e);
      this._drawEvents();
      return;
    }
    if (seq !== this._srSeq) return;
    this._search.items = res.results;
    this._search.loading = false;
    this._drawEvents();
  }

  _endSearch() {
    clearTimeout(this._srTimer);
    this._srSeq++;
    if (!this._search) return;
    this._search = null;
    this._searchHead.hidden = true;
    this._syncTools();
    this._searchInput.value = "";
    this._drawEvents();
    this._fillRoom();
  }

  /** Highlight the events the playhead is in (on the cameras shown). */
  _markActiveEvent(t) {
    if (!this._evItemsEl) return;
    const s = this._search;
    const ids = (s ? this._searchShown() : this._evItems ?? [])
      .filter((e) => this._shown.includes(e.camera_id) && t >= e.start - 3 && t <= Math.max(e.end ?? t, e.start + 10))
      .map((e) => String(s ? e.key : e.id));
    const key = `${s ? "s" : "e"}:${ids.join()}`;
    if (key === this._activeEvent) return;
    this._activeEvent = key;
    for (const b of this._evItemsEl.querySelectorAll(".ev")) b.classList.toggle("on", ids.includes(b.dataset.sr ?? b.dataset.ev));
  }

  /** Play an event: its camera becomes the master, from a little before it. */
  _jumpToEvent(ev) {
    this._selectCamera(ev.camera_id, ev.start - 3, true);
    // On a phone the list is below the video; bring the video back into view.
    const r = this._stage.getBoundingClientRect();
    if (r.top < 0 || r.bottom > window.innerHeight) this._stage.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  _timeAt(e) {
    const r = this._track.getBoundingClientRect();
    const f = Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1);
    return this._view.start + f * (this._view.end - this._view.start);
  }

  _showHover(e) {
    if (e.pointerType !== "mouse" && !this._drag) return;
    const t = this._timeAt(e);
    const r = this._track.getBoundingClientRect();
    this._hover.hidden = false;
    this._hover.style.left = `${Math.min(Math.max(e.clientX - r.left, 30), r.width - 30)}px`;
    this._hover.textContent = fmtTime(t);
    if (this._drag) {
      this._ph.style.left = `${((t - this._view.start) / (this._view.end - this._view.start)) * 100}%`;
      this._clock.textContent = `${fmtDate(t)} · ${fmtTime(t)}`;
    }
  }

  /** Clock readout + playhead for wall time t. */
  _paint(t) {
    if (!this._clock || this._drag) return;
    const text = `${fmtDate(t)} · ${fmtTime(t)}`;
    this._clock.textContent = text;
    this._fsClock.textContent = fmtTime(t);
    const { start, end } = this._view;
    this._ph.style.display = t >= start && t <= end ? "" : "none";
    this._ph.style.left = `${((t - start) / (end - start)) * 100}%`;
    const live = this._isLive(t);
    this._liveTag.hidden = !live;
    this.shadowRoot.querySelector(".controls .live")?.classList.toggle("on", live);
    this._markActiveEvent(t);
  }

  /** Where to resume: live if it was playing (or about to play) the newest footage. */
  _wantsLive(t) {
    return this._isLive(t) || !!this._master?.goingLive || (!!this._master?.intendsPlay() && nowS() - t < LIVE_LAG + 20);
  }

  /** Playing (close to) the newest footage of a live playlist. */
  _isLive(t) {
    const m = this._master;
    // With MSE, the stream close to now (live, or recordings that caught up);
    // without, the growing recording.
    return MSE ? !!m?.feed && nowS() - t < 10 : !!m?.session?.live && nowS() - t < LIVE_LAG + 20;
  }

  /**
   * Size the stage so that everything from the camera chips to the timeline
   * fits on the screen: the cells keep 16:9 and the grid gets narrower
   * (centred) when the full width would be too tall.
   */
  _fit() {
    const st = this._stage;
    requestAnimationFrame(() => this._markScrolls()); // the list's height follows the layout
    if (!st || !this._main || this.shadowRoot.fullscreenElement) return;
    this._vw = window.innerWidth;
    this._vh = window.innerHeight;
    const n = Math.max(this._players.size, 1);
    const cols = n <= 1 ? 1 : n <= 4 ? 2 : 3;
    const rows = Math.ceil(n / cols);
    const gap = n > 1 ? 2 : 0;
    const width = this._main.clientWidth;
    if (!width) return;
    // Everything in the column except the stage, and where the card starts
    // on the page (below HA's toolbar), capped for cards further down.
    const chrome = this._main.offsetHeight - st.offsetHeight;
    const top = Math.min(Math.max(this.getBoundingClientRect().top + window.scrollY, 0), 200);
    // Room below the card's top: the event list beside a short main column
    // (a screen taller than the 16:9 video needs) reaches down to it.
    const room = `${Math.max(window.innerHeight - top - 8, 0)}px`;
    if (this.style.getPropertyValue("--room") !== room) this.style.setProperty("--room", room);
    const avail = Math.max(window.innerHeight - top - chrome - 8, MIN_STAGE_HEIGHT);
    const cellH = (avail - gap * (rows - 1)) / rows;
    const fitW = (cellH * 16) / 9 * cols + gap * (cols - 1);
    const w = Math.min(width, Math.floor(fitW));
    const px = w < width ? `${w}px` : "";
    if (st.style.width !== px) st.style.width = px;
  }

  _onTime() {
    const m = this._master;
    if (!m?.session || !m.mediaReady) return;
    const t = m.wall();
    m.target = t;
    const { start, end } = this._view;
    const following = !this._drag && Date.now() > this._followPausedUntil;
    // Live: the timeline scrolls along with now, kept near the right edge
    // (redrawn once it has moved about a pixel), rather than sitting still
    // until now runs off it. Not under the pointer: the bars would be replaced
    // under a tooltip and the hover time would go stale. A bigger move (back
    // from a pan) is a new range to fetch, and so is what's recording, every
    // few seconds (_liveRecsDue).
    if (following && !this._overTrack && this._isLive(t)) {
      const e = nowS() + this._span * 0.05;
      const shift = Math.abs(e - end);
      const due = this._liveRecsDue();
      if (due || shift > this._span / Math.max(this._track.clientWidth, 200)) {
        this._view = { start: e - this._span, end: e };
        if (due || shift > this._span * 0.1) this._loadTimeline();
        else this._drawTimeline(); // paints t too
        return;
      }
    }
    if (following && (t < start || t > end)) {
      const s = t - this._span * 0.2;
      const e = Math.min(s + this._span, nowS() + this._span * 0.05);
      this._view = { start: e - this._span, end: e };
      this._loadTimeline();
      return;
    }
    this._paint(t);
  }

  // Following live on a short span: time to look again what is recording
  // (not while a load is still out: a slow SS would get them piling up, each
  // one discarding the last).
  _liveRecsDue() {
    return (
      this._span <= LIVE_RECS_SPAN &&
      !document.hidden &&
      !this._tlBusy &&
      Date.now() - (this._tlAt || 0) > LIVE_RECS_MS
    );
  }

  _periodic() {
    if (this._view.end > nowS() - this._span) this._loadTimeline();
    this._refreshEvents();
  }
}

// The card is registered as a Lovelace resource by the integration. Loaded
// early (e.g. before HA's app bundle swaps window.customElements for its
// scoped-registry polyfill), a definition would land in the wrong registry,
// so wait for the frontend's root element and define on the current one.
async function register() {
  // hc-main on Home Assistant Cast (a Chromecast / Nest Hub showing a dashboard).
  await Promise.race(["home-assistant", "hc-main"].map((tag) => window.customElements.whenDefined(tag)));
  const registry = window.customElements;
  if (registry.get(CARD_TAG)) return;
  registry.define(CARD_TAG, SSTimelineCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TAG,
    name: "Surveillance Station timeline",
    description: "Play back Synology Surveillance Station recordings with a timeline and bookmarks.",
  });
  console.info(`%c SS-TIMELINE-CARD %c ${CARD_VERSION} `, "background:#1976d2;color:#fff", "");
  // The time-lapse card rides along (same version, no resource of its own).
  import(new URL(`./ss-timelapse-card.js?v=${CARD_VERSION}`, import.meta.url).href).catch((e) =>
    console.error("ss-timelapse-card failed to load", e)
  );
}
register();
