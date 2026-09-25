/*
 * ss-timeline-card: play back Synology Surveillance Station recordings with a
 * scrubbable wall-clock timeline and the SS bookmarks laid on top of it.
 *
 * Talks to the surveillance_station integration over the HA WebSocket:
 *   surveillance_station/cameras | recordings | bookmarks | bookmark_page |
 *   live | vod | vod_runs
 * Live is SS's real-time stream (`live`: a single-use WebSocket URL, played
 * through MSE by LiveFeed). Recordings are HLS: `vod` returns a playlist URL
 * plus `runs`, the map between playlist time and wall-clock time (a new run
 * starts after every gap in the recordings).
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
 *   cameras:  names or ids to show (default: all); the viewer's last choice
 *             (the camera chips) wins
 *   camera:   name or id of the camera to start on (the master)
 *   span:     timeline width in seconds (default 3600); the viewer's last choice wins
 *   clock:    false hides the time overlay; the viewer's last choice wins
 *   entry_id: which Surveillance Station entry, if there is more than one
 * URL parameters override on load: ?ss_camera=<name|id>&ss_time=<epoch seconds>
 */

const CARD_TAG = "ss-timeline-card";
const CARD_VERSION = "0.7.0";
const HLS_URL = new URL("./vendor/hls.light.min.mjs", import.meta.url).href;

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
const FOLLOW_PAUSE_MS = 15_000; // after a manual pan, don't snap the view back
// A transparent poster: without one, Android WebView (the HA app) paints its
// default poster, a big grey play arrow, over a video with no frame yet, so
// it flashed on every jump to a bookmark or new window.
const BLANK_POSTER = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
const THUMB_RETRY_MS = 30_000;
const EVENT_PAGE = 30; // events per page of the list; more load as it scrolls
// The list (and its thumbnail links, valid ~24 h) is reloaded after this long.
const EVENT_LIST_MAX_AGE_MS = 12 * 3600 * 1000;
const TICK_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
// One colour per camera (chip, cell label, timeline pins, event rows); these
// read on both light and dark backgrounds.
const CAM_COLORS = ["#4f8ff7", "#f5a623", "#2dbd8f", "#e5534b", "#a371f7", "#e05aa8", "#1fb5c6", "#c9a227"];
const MIN_STAGE_HEIGHT = 160; // px; below this the page scrolls instead
// Grid sync: how often followers correct, and how.
const SYNC_MS = 500;
const DRIFT_JUMP = 2; // seconds off: seek
const DRIFT_NUDGE = 0.1; // seconds off: speed up / slow down (at most ±20%)
// hls.js buffers, seconds (a camera is 3-5 Mbit/s). The master keeps some
// history for instant -10 s / -30 s; followers only need to keep up, and
// their read-ahead competes with the master's for the server's fetch slots.
const BUFFER = {
  master: { maxBufferLength: 30, maxMaxBufferLength: 30, backBufferLength: 30 },
  follower: { maxBufferLength: 12, maxMaxBufferLength: 12, backBufferLength: 10 },
};
const ZOOM_MAX = 8;
const FS_IDLE_MS = 3000; // fullscreen controls hide after this

let hlsPromise;
const loadHls = () =>
  (hlsPromise ??= import(HLS_URL).then(
    (m) => m.default,
    (e) => {
      hlsPromise = null; // try again next time (flaky network in the app)
      throw e;
    }
  ));

const nowS = () => Date.now() / 1000;
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

function fmtTime(t, seconds = true) {
  const d = new Date(t * 1000);
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
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${fmtTime(t)}`;
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
    /* The list is as tall as the main column and scrolls inside it. */
    .side { height: 0; min-height: 100%; border-left: 1px solid var(--divider-color); border-top: none; }
    .ev-list { max-height: none; }
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
  .stage.noclock .clock { display: none; }
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
  .ev-list { flex: 1; min-height: 0; overflow-y: auto; padding: 0 8px 8px; overscroll-behavior: contain; max-height: 60vh; }
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

/**
 * Plays one camera: its own HLS session over a window of wall-clock time,
 * loading veil, and the wall <-> media mapping for that session.
 */
// ---- real-time stream ------------------------------------------------------

// Seconds of video held ahead of the playhead on a live stream: the jitter
// margin (Wi-Fi cameras deliver frames in bursts). Latency is about this.
const LIVE_TARGET = 0.8;
const LIVE_JUMP = 2.5; // further behind than target + this: jump to the edge
const LIVE_QUEUE_MAX = 90; // fragments waiting to be appended: drop to the next keyframe

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
    // SS's fragment timestamps don't survive reconnects or Wi-Fi hiccups;
    // appended in order, each fragment follows the last.
    this.sb.mode = "sequence";
    this.sb.addEventListener("updateend", () => this.pump());
    this.sb.addEventListener("error", () => this.fail());
    for (const part of parts) this.push(part, null);
    return true;
  }

  push(data, meta) {
    if (this.closed) return;
    this.queue.push([data, meta]);
    this.pump();
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
    const next = this.queue.shift();
    if (!next) return;
    try {
      this.pending = next[1];
      sb.appendBuffer(next[0]);
    } catch (e) {
      this.pending = null;
      if (e.name === "QuotaExceededError") {
        this.queue = [];
        this.overflow = true; // the feed starts over at a keyframe
      } else this.fail();
    }
  }

  end() {
    const b = this.sb?.buffered;
    return b?.length ? b.end(b.length - 1) : 0;
  }

  close() {
    this.closed = true;
    this.queue = [];
    URL.revokeObjectURL(this.url);
  }
}

/**
 * A camera's real-time stream (SS's WebSocket stream relayed by HA) played
 * through MSE into the player's <video>. Keeps LIVE_TARGET seconds ahead of
 * the playhead, gently (playback speed) or, when far behind, by jumping.
 * Sound, only when wanted, goes through its own <audio>: a video that waits
 * for audio would stall on every burst.
 */
class LiveFeed {
  constructor(player, url, { onStart, onEnd }) {
    this.player = player;
    this.video = player.video;
    this.onStart = onStart;
    this.onEnd = onEnd;
    this.map = []; // per video fragment: [media start, media end, wall time of its frame]
    this.keys = []; // media start of each keyframe fragment
    this.waitKey = true; // dropping until the next keyframe
    this.audio = null;
    this.audioInit = null;
    this.audioCodecOk = true;
    this.sink = new MseSink(this.video, {
      onAppended: (meta) => this.appended(meta),
      trimEnd: (t) => this.keyBefore(t - 8),
      onError: () => this.end("error"),
    });
    // Attaching reset the rate to the card's playback speed; live runs at 1x.
    this.video.defaultPlaybackRate = this.video.playbackRate = 1;
    this.events = new AbortController();
    const on = (type, fn) => this.video.addEventListener(type, fn, { signal: this.events.signal });
    on("error", () => this.end("error"));
    // Paused live: the sound pauses too; playing again catches both up.
    on("pause", () => this.audio?.el.pause());
    on("play", () => this.resumeAudio());
    this.ws = new WebSocket(url);
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (e) => this.message(e.data);
    this.ws.onclose = () => this.end("closed");
    this.ws.onerror = () => {};
    this.keepAlive = setInterval(() => this.ws.readyState === 1 && this.ws.send("keepAlive"), 10000);
    this.control = setInterval(() => this.steer(), 500);
  }

  /** The stream is over (closed, refused, undecodable); the player decides what next. */
  end(why, detail) {
    if (this.closed || this.ended) return;
    this.ended = true;
    this.onEnd?.(why, detail);
  }

  message(buf) {
    const msg = readStreamMsg(buf);
    if (!msg || this.closed || this.ended) return;
    const { head, data } = msg;
    if (head.close) return this.end("closed");
    if (head.vdoCodec || head.adoCodec) {
      if (head.vdoCodec && !/^(H26[45]|AVC1)$/i.test(head.vdoCodec)) return this.end("codec", head.vdoCodec);
      this.audioCodecOk = /^(MPEG4-GENERIC|MP4A-LATM)$/i.test(head.adoCodec ?? ""); // AAC
      return;
    }
    const video = head.mediaType === "1";
    const box = String.fromCharCode(...data.subarray(4, 8));
    if (box === "ftyp") {
      this[video ? "vFtyp" : "aFtyp"] = data.slice();
      return;
    }
    if (box === "moov") {
      if (!video) {
        this.audioInit = [this.aFtyp, data.slice()];
        if (this.audio && !this.audio.inited) this.initAudio();
        return;
      }
      // A new init mid-stream (e.g. the camera changed resolution): start over.
      if (this.videoInit) return this.end("error");
      this.videoInit = true;
      this.initVideo(this.vFtyp, data.slice());
      return;
    }
    if (!video) {
      const a = this.audio;
      if (a?.sink.sb && !this.video.paused) {
        if (a.sink.queue.length >= LIVE_QUEUE_MAX) a.sink.queue = [];
        a.sink.push(data.slice(), {});
      }
      return;
    }
    if (!this.sink.sb) return;
    const key = head.key === "1";
    // Paused, or the browser fell behind: skip to the next keyframe.
    if (this.video.paused && this.started) this.waitKey = true;
    if (this.sink.queue.length >= LIVE_QUEUE_MAX || this.sink.overflow) {
      this.sink.queue = [];
      this.sink.overflow = false;
      this.waitKey = true;
    }
    // (Before the first frame the video is paused: that's not a pause.)
    if (this.waitKey && !(key && (!this.started || !this.video.paused))) return;
    this.waitKey = false;
    this.sink.push(data.slice(), { wall: Math.min(Number(head.msec) / 1000, nowS()), key });
  }

  async initVideo(ftyp, moov) {
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
    if (!codec || !MSE.isTypeSupported(mime)) return this.end("codec", codec);
    await this.sink.init([ftyp, moov], mime);
  }

  appended(meta) {
    const end = this.sink.end();
    // In sequence mode a fragment starts where the previous one ended.
    const start = this.map.at(-1)?.[1] ?? Math.max(0, end - 0.1);
    this.map.push([start, end, meta.wall]);
    if (meta.key) this.keys.push(start);
    if (this.map.length > 600) this.map.splice(0, this.map.length - 600);
    if (this.keys.length > 200) this.keys.splice(0, this.keys.length - 200);
    if (!this.started && end > LIVE_TARGET) {
      this.started = true;
      this.video.currentTime = Math.max(0, end - LIVE_TARGET);
      this.onStart?.();
    }
  }

  /** The last keyframe at or before media time t (or -Infinity). */
  keyBefore(t) {
    for (let i = this.keys.length - 1; i >= 0; i--) if (this.keys[i] <= t) return this.keys[i];
    return -Infinity;
  }

  /** Wall time shown at media time m (from the frames' own timestamps). */
  wall(m) {
    const map = this.map;
    if (!map.length) return null;
    // The fragment holding m: the last one starting at or before it.
    let lo = 0;
    let hi = map.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (map[mid][0] <= m) lo = mid;
      else hi = mid - 1;
    }
    const [start, , wall] = map[lo];
    return wall + Math.max(0, m - start);
  }

  /** Hold the target margin: nudge the speed, jump if far behind. */
  steer() {
    const hold = (el, sink) => {
      const ahead = sink.end() - el.currentTime;
      let rate = 1;
      if (ahead > LIVE_TARGET + LIVE_JUMP) el.currentTime = sink.end() - LIVE_TARGET;
      else if (ahead > LIVE_TARGET + 0.3) rate = 1.1;
      else if (ahead < LIVE_TARGET - 0.3) rate = 0.93;
      if (el.playbackRate !== rate) el.playbackRate = rate;
    };
    const v = this.video;
    if (this.started && !v.paused && !v.seeking) hold(v, this.sink);
    const a = this.audio;
    if (a?.started && !a.el.paused && !a.el.seeking) hold(a.el, a.sink);
  }

  /** Sound on (the master, unmuted) or off. */
  setAudio(on) {
    if (on && !this.audio && this.audioCodecOk) this.startAudio();
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
    a.sink = new MseSink(el, {
      onAppended: () => this.audioAppended(a),
      onError: () => this.audio === a && this.stopAudio(),
    });
    el.play().catch(() => {});
    if (this.audioInit) this.initAudio();
  }

  async initAudio() {
    const a = this.audio;
    if (!a || a.inited) return;
    a.inited = true;
    const mime = 'audio/mp4; codecs="mp4a.40.2"';
    const ok = MSE.isTypeSupported(mime) && (await a.sink.init(this.audioInit, mime));
    if (!ok && this.audio === a) this.stopAudio();
  }

  audioAppended(a) {
    if (a.started || a.sink.end() <= LIVE_TARGET) return;
    a.started = true;
    a.el.currentTime = Math.max(0, a.sink.end() - LIVE_TARGET);
    if (!this.video.paused) a.el.play().catch(() => {});
  }

  resumeAudio() {
    const a = this.audio;
    if (!a?.started) return;
    a.el.currentTime = Math.max(0, a.sink.end() - LIVE_TARGET);
    a.el.play().catch(() => {});
  }

  stopAudio() {
    const a = this.audio;
    if (!a) return;
    this.audio = null;
    a.sink.close();
    a.el.pause();
    a.el.removeAttribute("src");
    a.el.load();
  }

  close() {
    this.closed = true;
    this.events.abort();
    clearInterval(this.keepAlive);
    clearInterval(this.control);
    this.ws.onmessage = this.ws.onclose = null;
    try {
      this.ws.close();
    } catch (e) {
      /* already closed */
    }
    this.stopAudio();
    this.sink.close();
  }
}

function findBox(buf, tag) {
  const t = [...tag].map((c) => c.charCodeAt(0));
  for (let i = 4; i + 4 <= buf.length; i++)
    if (buf[i] === t[0] && buf[i + 1] === t[1] && buf[i + 2] === t[2] && buf[i + 3] === t[3]) return i;
  return -1;
}

class Player {
  constructor(card, cameraId) {
    this.card = card;
    this.cameraId = cameraId;
    this.hls = null;
    this.feed = null; // the real-time stream, when playing live
    this.session = null; // last vod response for the loaded window ({live, ws} for the stream)
    this.mediaReady = false;
    this.target = nowS() - LIVE_LAG; // wall time we want / are at when nothing is playing
    this.seq = 0;
    this.loading = false;
    this.veilKind = null;
    this.retry = null;

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
      if (this.veilKind === "loading") this.setStatus("");
      this.still.classList.remove("show");
    };
    v.addEventListener("playing", () => {
      this.expiredRetry = false;
      ready();
    });
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
    this.veilKind = text ? kind : null;
    this.retry = retry;
    setVeil(this.veil, text, kind, sub);
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

  // ---- time mapping -------------------------------------------------------

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

  bufferProfile() {
    return BUFFER[this.card._master === this ? "master" : "follower"];
  }

  /** hls.js reads these live, so a new master buffers more from now on. */
  applyBufferProfile() {
    if (this.hls) Object.assign(this.hls.config, this.bufferProfile());
  }

  destroyMedia() {
    if (this.feed) {
      this.feed.close();
      this.feed = null;
    }
    if (this.hls) {
      this.hls.destroy();
      this.hls = null;
    }
    this.mediaReady = false;
    const v = this.video;
    v.pause();
    v.removeAttribute("src");
    v.load();
  }

  destroy() {
    this.seq++;
    this.goingLive = false;
    clearTimeout(this.bufTimer);
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
    if (MSE && isLiveTime(t)) {
      // Already live (or connecting): just the play intent.
      if (this.feed || this.goingLive) {
        this.autoplay = autoplay;
        if (!this.loading) autoplay ? this.video.play().catch(() => {}) : this.video.pause();
        return;
      }
      return this.goLive(autoplay);
    }
    if (this.feed || this.goingLive) return this.load(t, autoplay); // back to recordings
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
   * Load a playback window around wall time t.
   * `after`: when continuing past the end of a window, the wall time the new
   * window must get beyond; otherwise playback stops instead of looping.
   */
  async load(t, autoplay, after = null) {
    if (MSE && after == null && isLiveTime(t)) return this.goLive(autoplay);
    this.goingLive = false;
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
    let Hls;
    try {
      Hls = await loadHls();
    } catch (e) {
      if (seq === this.seq) {
        this.loading = false;
        this.session = null;
        this.setStatus("Playback failed", "error", errText(e));
      }
      return;
    }
    if (seq !== this.seq) return;
    this.loading = false;
    const v = this.video;
    v.playbackRate = v.defaultPlaybackRate = card._rate;

    if (!Hls.isSupported()) {
      if (v.canPlayType("application/vnd.apple.mpegurl")) {
        // Safari without MSE: native HLS.
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
      } else {
        this.setStatus("This browser can't play HLS video", "error");
      }
      return;
    }

    const hls = new Hls({ startPosition: start, ...this.bufferProfile() });
    this.hls = hls;
    let recovered = false;
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (this.autoplay) v.play().catch(() => {});
    });
    // A live playlist grows on every reload, and may gain a gap; refresh the
    // wall-clock mapping with it.
    const token = res.url.split("/").at(-2);
    hls.on(Hls.Events.LEVEL_UPDATED, async () => {
      const s = this.session;
      if (hls !== this.hls || !s?.live) return;
      try {
        const upd = await card._ws({ type: "surveillance_station/vod_runs", token });
        if (hls === this.hls && this.session === s) Object.assign(s, upd);
      } catch (e) {
        /* the next reload retries */
      }
    });
    hls.on(Hls.Events.ERROR, (_, d) => {
      if (!d.fatal || hls !== this.hls) return;
      if (d.type === Hls.ErrorTypes.MEDIA_ERROR && !recovered) {
        recovered = true;
        hls.recoverMediaError();
        return;
      }
      const wall = this.wall();
      const playing = !v.paused || this.autoplay;
      this.destroyMedia();
      this.session = null;
      // 404: the session expired or was evicted; start a fresh one once.
      if (d.response?.code === 404 && !this.expiredRetry) {
        this.expiredRetry = true;
        this.load(wall, playing);
        return;
      }
      const codec = /codec/i.test(d.details) || d.details === "manifestIncompatibleCodecsError";
      this.setStatus(codec ? "This browser can't decode H.265 (HEVC) video" : "Playback error", "error", codec ? "" : d.details);
    });
    hls.loadSource(res.url);
    hls.attachMedia(v);
  }

  /** Play the camera's real-time stream (see LiveFeed). */
  async goLive(autoplay) {
    const seq = ++this.seq;
    const card = this.card;
    this.target = nowS();
    this.autoplay = autoplay;
    this.lastLoad = { at: Date.now(), wall: this.target };
    if (card._master === this) card._paint(this.target);
    this.freeze();
    this.video.pause();
    this.setStatus("Connecting", "loading", `${card._cameraName(this.cameraId)} · Live`);
    this.loading = true;
    this.goingLive = true;
    let res;
    try {
      res = await card._ws({ type: "surveillance_station/live", camera_id: this.cameraId });
    } catch (e) {
      if (seq === this.seq) {
        this.loading = this.goingLive = false;
        this.setStatus("Live view failed", "error", errText(e), () => this.goLive(true));
      }
      return;
    }
    if (seq !== this.seq) return;
    this.destroyMedia();
    this.gap = false;
    this.session = { live: true, ws: true };
    const url = card._hass.hassUrl(res.url).replace(/^http/, "ws");
    const feed = new LiveFeed(this, url, {
      onStart: () => {
        if (feed !== this.feed) return;
        this.loading = this.goingLive = false;
        this.liveStartedAt = Date.now();
        if (this.autoplay) this.video.play().catch(() => {});
      },
      onEnd: (why, detail) => {
        if (feed !== this.feed) return;
        const playing = this.intendsPlay();
        const started = feed.started;
        this.destroyMedia();
        this.loading = this.goingLive = false;
        this.session = null;
        const retry = () => {
          this.liveDrops = 0;
          this.liveFailed = false;
          this.goLive(true);
        };
        if (why === "codec") {
          this.liveFailed = true;
          this.setStatus("This browser can't decode this camera's video", "error", detail ?? "", retry);
          return;
        }
        // A dropped stream reconnects, backing off (1, 2, 4 s) while it keeps
        // failing straight away, or never starts (refused, SS unreachable).
        const quick = !started || Date.now() - this.liveStartedAt < 10000;
        this.liveDrops = quick ? (this.liveDrops ?? 0) + 1 : 1;
        if (this.liveDrops > 3) {
          this.liveFailed = true;
          this.setStatus("Live view unavailable", "error", this.card._cameraName(this.cameraId), retry);
          return;
        }
        this.setStatus("Reconnecting", "loading", `${this.card._cameraName(this.cameraId)} · Live`);
        const at = this.seq;
        setTimeout(() => at === this.seq && this.goLive(playing), 1000 * 2 ** (this.liveDrops - 1));
      },
    });
    this.feed = feed;
    this.syncAudio();
  }

  /** Live sound: from the master, when not muted. */
  syncAudio() {
    this.feed?.setAudio(this.card._master === this && !this.video.muted);
  }

  /** At the end of a window, carry on into whatever was recorded next. */
  async continue() {
    // "ended" means the playlist is closed: an old window, or a live one that
    // hit the server's window cap.
    const s = this.session;
    if (!s) return;
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
      if (!this.feed) {
        const last = this.lastLoad;
        const wasLive = last && isLiveTime(last.wall + (Date.now() - last.at) / 1000);
        if (!this.liveFailed && (!last || Date.now() - last.at > 5000 || !wasLive)) this.goLive(playing);
        return;
      }
      const v = this.video;
      if (playing && v.paused) v.play().catch(() => {});
      else if (!playing && !v.paused) v.pause();
      return;
    }
    if (this.feed) {
      this.load(wall, playing);
      return;
    }
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
    this._prevShown = null; // what the grid button goes back to
    this._evItems = []; // event list: bookmarks of the shown cameras, newest first, as loaded
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
    // HA came back (e.g. restarted): its playback sessions and thumbnail
    // links are gone, and events may have been missed meanwhile. HA's own
    // dashboards usually rebuild their cards on reconnect (this one is then
    // disconnected by the time the timer fires); a card that stays catches up.
    this._onReconnect = () => setTimeout(() => this._reconnected(), 1000);
    // The date/time popover closes on Escape or a tap anywhere else.
    this._onDocDown = (e) => {
      if (this._jumpBox && !this._jumpBox.hidden && !e.composedPath().some((n) => n.classList?.contains("jumpwrap")))
        this._jumpBox.hidden = true;
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
    this._showClock = prefs.get("clock", this._config.clock !== false);
  }

  set hass(hass) {
    const first = !this._hass;
    if (hass.connection !== this._hass?.connection) {
      this._hass?.connection?.removeEventListener?.("ready", this._onReconnect);
      if (this.isConnected) hass.connection?.addEventListener?.("ready", this._onReconnect);
    }
    this._hass = hass;
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
      p.goingLive = false;
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
    if (!this._cameras.length) {
      this._stageMessage("No enabled cameras in Surveillance Station", "empty");
      return;
    }
    this._stageMessage("");
    const params = new URLSearchParams(location.search);
    this._shown = this._initialShown();
    this._prevShown = prefs.get("cameras_prev", null);
    // A camera asked for (e.g. by a notification link) is added to the shown ones.
    const asked = this._findCamera(params.get("ss_camera") ?? this._config.camera);
    const cam = asked ?? this._cameras.find((c) => this._shown.includes(c.id));
    const t = Number(params.get("ss_time"));
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

  /** Cameras shown: the viewer's last pick, else the `cameras` option, else all. */
  _initialShown() {
    const known = (ids) => ids.filter((id) => this._cameras.some((c) => c.id === id));
    const saved = prefs.get("cameras", null);
    let ids = Array.isArray(saved) ? known(saved) : [];
    if (!ids.length && Array.isArray(this._config.cameras)) {
      ids = this._config.cameras.map((x) => this._findCamera(x)?.id).filter((id) => id != null);
    }
    return ids.length ? ids : this._cameras.map((c) => c.id);
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
              <button class="icon" data-act="clock" title="Show / hide the time on the video">${icon("mdi:clock-outline")}</button>
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
    this._evList = $(".ev-list");
    this._evItemsEl = $(".ev-items");
    this._evFoot = $(".ev-foot");
    this._when = $(".when");
    this._jumpBox = $(".jump");

    if (hevcSupport() === false) {
      $(".warn").textContent =
        "This browser reports no H.265 (HEVC) support; playback of Surveillance Station recordings will likely fail here.";
    }

    root.querySelector("ha-card").addEventListener("click", (e) => this._onClick(e));
    $(".speed").addEventListener("change", (e) => {
      this._rate = Number(e.target.value);
      for (const p of this._players.values()) p.video.playbackRate = p.video.defaultPlaybackRate = this._rate;
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
    tr.addEventListener("pointerleave", () => {
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
    this._jumpBox.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this._jumpBox.hidden = true;
    });

    this._markSpan();
    this._applyClock();
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
      b.title = n > 1 ? "Only this camera" : "Back to the grid";
      b.querySelector("ha-icon").setAttribute("icon", n > 1 ? "mdi:square-outline" : "mdi:view-grid-outline");
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
    for (const q of this._players.values()) {
      q.el.classList.toggle("master", q === p);
      if (q !== p) q.video.muted = true;
      q.applyBufferProfile();
    }
    p.video.muted = muted;
    for (const q of this._players.values()) q.syncAudio();
    // A follower may have been mid-nudge; the master sets the pace.
    p.video.playbackRate = this._rate;
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
  _setShown(ids, master = this._cameraId, at = null, autoplay = null) {
    const m = this._master;
    if (!m) return;
    const wall = at ?? m.wall();
    const playing = autoplay ?? m.intendsPlay();
    const before = [...this._shown];
    this._shown = [...new Set([...ids, master])];
    this._cameraId = master;
    const added = this._buildPlayers();
    prefs.set("cameras", this._shown);
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

  /** Chip: show / hide a camera. The last one stays. */
  _toggleCamera(id) {
    if (!this._shown.includes(id)) return this._setShown([...this._shown, id]);
    if (this._shown.length === 1) return;
    const rest = this._shown.filter((x) => x !== id);
    this._setShown(rest, id === this._cameraId ? rest[0] : this._cameraId);
  }

  /** Just the master, or back to the cameras shown before (else all). */
  _toggleSolo() {
    if (this._shown.length > 1) {
      this._prevShown = this._shown;
      prefs.set("cameras_prev", this._prevShown);
      this._setShown([this._cameraId]);
    } else {
      const back = this._prevShown?.filter((id) => this._cameras.some((c) => c.id === id));
      this._setShown(back?.length > 1 ? back : this._cameras.map((c) => c.id));
    }
  }

  /** Make a camera the master (showing it if needed) and go to t. */
  _selectCamera(id, t, autoplay) {
    if (this._shown.includes(id)) this._setMaster(id);
    else this._setShown([...this._shown, id], id, t, autoplay); // _seekAll then finds it loading there
    this._centerOn(t);
    this._loadTimeline();
    this._seekAll(t, autoplay);
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
    if (m.feed || m.goingLive) {
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
        this._toggleSolo();
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
    this.shadowRoot.querySelector('[data-act="mute"] ha-icon')?.setAttribute("icon", muted ? "mdi:volume-off" : "mdi:volume-high");
  }

  _applyClock() {
    this._stage.classList.toggle("noclock", !this._showClock);
    this.shadowRoot.querySelector('.controls [data-act="clock"]')?.classList.toggle("on", this._showClock);
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
      this._toggleCamera(id);
      return;
    }
    if (b.dataset.act === "events") {
      const open = this._layoutEl.classList.toggle("noside") === false;
      prefs.set("events", open);
      this._fit();
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
        this._toggleSolo();
        break;
      case "clock":
        this._showClock = !this._showClock;
        prefs.set("clock", this._showClock);
        this._applyClock();
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
    const { start, end } = this._view;
    this._drawTimeline();
    if (!this._shown.length) return;
    const shown = [...this._shown];
    const q = { start: Math.floor(start), end: Math.ceil(end) };
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
      if (seq === this._tlSeq) this._rangeEl.textContent = `Timeline failed: ${errText(e)}`;
      return;
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
    for (const [s, e] of unionOf(this._recs.map((r) => [r.start, r.live ? now : r.end]))) {
      if (e < start || s > end) continue;
      const a = Math.max(x(s), 0);
      const b = Math.min(x(e), 100);
      html += `<div class="rec" style="left:${a}%;width:${Math.max(b - a, 0.1)}%"></div>`;
    }
    let prev = -Infinity;
    let dense = false;
    for (const bm of this._bookmarks) {
      if (bm.end < start || bm.start > end) continue;
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
    this._rangeEl.innerHTML = `<span class="long">${fmtDay(start)} ${fmtTime(start, false)} – ${
      fmtDay(end) === fmtDay(start) ? "" : fmtDay(end) + " "
    }${fmtTime(end, false)}</span><span class="short">${short}</span>`;
    this._paint(this._currentWall());
  }

  _currentWall() {
    return this._master ? this._master.wall() : nowS() - LIVE_LAG;
  }

  // ---- event list -------------------------------------------------------

  /** Start the list over (the cameras shown changed, or first load). */
  _resetEvents() {
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
    if (this._evLoading || !this._evMore || !this._shown.length) return;
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
    this._drawEvents();
    // Still room on screen (a tall sidebar): keep going.
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
      res = await this._ws({ type: "surveillance_station/bookmark_page", camera_ids: this._shown, limit: EVENT_PAGE });
    } catch (e) {
      return;
    }
    if (seq !== this._evSeq || this._evLoading) return;
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
    this._evFoot.textContent = this._evError
      ? `Couldn't load events: ${this._evError}`
      : this._evLoading
        ? "Loading…"
        : this._evItems.length
          ? this._evMore ? "" : "No earlier events"
          : `No bookmarks for ${this._shown.length === 1 ? this._cameraName(this._shown[0]) : "these cameras"}`;
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
    for (const e of this._evItems) {
      const d = new Date(e.start * 1000).toDateString();
      if (d !== day) {
        day = d;
        const label = d === today ? "Today" : d === yesterday ? "Yesterday" : fmtDate(e.start);
        node(`d:${d}:${label}`, `<div class="ev-day">${label}</div>`);
      }
      const dur = e.end > e.start ? fmtDur(e.end - e.start) : "";
      const thumb = e.thumbnail ? `<img loading="lazy" decoding="async" alt="" src="${esc(e.thumbnail)}">` : "";
      node(
        `e:${e.id}:${e.start}:${e.end}:${e.camera_id}:${e.name}:${e.comment}`,
        `<button class="ev" data-ev="${e.id}" style="--cam:${this._camColor(e.camera_id)}">
          <span class="thumb"><ha-icon icon="mdi:cctv"></ha-icon>${thumb}${dur ? `<span class="dur">${dur}</span>` : ""}</span>
          <span class="evt">
            <span class="n">${esc(e.name || "(unnamed)")}</span>
            <span class="m"><i></i>${esc(this._cameraName(e.camera_id))} · ${fmtTime(e.start)}</span>
            ${e.comment ? `<span class="c">${esc(e.comment)}</span>` : ""}
          </span>
        </button>`
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
    const total = this._evTotal ?? "";
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
  }

  /** Highlight the events the playhead is in (on the cameras shown). */
  _markActiveEvent(t) {
    if (!this._evItemsEl) return;
    const ids = (this._evItems ?? [])
      .filter((e) => this._shown.includes(e.camera_id) && t >= e.start - 3 && t <= Math.max(e.end, e.start + 10))
      .map((e) => String(e.id));
    const key = ids.join();
    if (key === this._activeEvent) return;
    this._activeEvent = key;
    for (const b of this._evItemsEl.querySelectorAll(".ev")) b.classList.toggle("on", ids.includes(b.dataset.ev));
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
    // With MSE, live is the real-time stream; without, the growing recording.
    return MSE ? !!m?.feed && nowS() - t < 10 : !!m?.session?.live && nowS() - t < LIVE_LAG + 20;
  }

  /**
   * Size the stage so that everything from the camera chips to the timeline
   * fits on the screen: the cells keep 16:9 and the grid gets narrower
   * (centred) when the full width would be too tall.
   */
  _fit() {
    const st = this._stage;
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
    if (!this._drag && Date.now() > this._followPausedUntil && (t < start || t > end)) {
      const s = t - this._span * 0.2;
      const e = Math.min(s + this._span, nowS() + this._span * 0.05);
      this._view = { start: e - this._span, end: e };
      this._loadTimeline();
      return;
    }
    this._paint(t);
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
  await window.customElements.whenDefined("home-assistant");
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
}
register();
