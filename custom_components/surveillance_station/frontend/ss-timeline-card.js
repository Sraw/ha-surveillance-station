/*
 * ss-timeline-card: play back Synology Surveillance Station recordings with a
 * scrubbable wall-clock timeline and the SS bookmarks laid on top of it.
 *
 * Talks to the surveillance_station integration over the HA WebSocket:
 *   surveillance_station/cameras | recordings | bookmarks | vod | vod_runs
 * `vod` returns an HLS playlist URL plus `runs`, the map between playlist time
 * and wall-clock time (a new run starts after every gap in the recordings).
 *
 * One Player per camera shown; one camera shown is the single view, several
 * are the grid. One player is the master: it has the sound and the clock and
 * the others follow its wall-clock time (see Player.follow). The timeline and
 * the event list cover the cameras shown.
 *
 * Card options (all optional):
 *   cameras:  names or ids to show (default: all); the viewer's last choice
 *             (the camera chips) wins
 *   camera:   name or id of the camera to start on (the master)
 *   span:     timeline width in seconds (default 3600)
 *   clock:    false hides the time overlay; the viewer's last choice wins
 *   entry_id: which Surveillance Station entry, if there is more than one
 * URL parameters override on load: ?ss_camera=<name|id>&ss_time=<epoch seconds>
 */

const CARD_TAG = "ss-timeline-card";
const CARD_VERSION = "0.5.0";
const HLS_URL = new URL("./vendor/hls.light.min.mjs", import.meta.url).href;

const SPANS = [
  [900, "15m"],
  [3600, "1h"],
  [6 * 3600, "6h"],
  [24 * 3600, "24h"],
];
const SPEEDS = [1, 2, 4, 8];
const PRE_ROLL = 30; // seconds before the target included in a new window
const WINDOW_AHEAD = 3600; // seconds after the target included in a new window
const LIVE_LAG = 20; // "live" plays this far behind now (segments close at now-5)
const REFRESH_MS = 60_000;
const FOLLOW_PAUSE_MS = 15_000; // after a manual pan, don't snap the view back
// Event list ranges: [seconds, label].
const EVENT_RANGES = [
  [86400, "24h"],
  [3 * 86400, "3d"],
  [7 * 86400, "7d"],
];
const TICK_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];
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
  ha-card { overflow: hidden; }
  .head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 12px 12px 8px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  [hidden] { display: none !important; }
  button { font: inherit; color: var(--primary-text-color); background: var(--secondary-background-color);
    border: 1px solid var(--divider-color); border-radius: 16px; padding: 4px 12px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 4px; min-height: 32px; }
  button:hover { border-color: var(--primary-color); }
  /* Camera chips: outlined = shown, filled = master. */
  .cams button.shown { border-color: var(--primary-color); color: var(--primary-color); }
  button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  button.icon { padding: 4px 8px; border-radius: 50%; min-width: 36px; justify-content: center; }
  ha-icon { --mdc-icon-size: 20px; }

  /* Stage: one cell per player; "single" shows one, "grid" all of them. */
  .stage { position: relative; background: #000; user-select: none; -webkit-user-select: none; }
  .stage.grid { display: grid; grid-template-columns: repeat(var(--cols, 2), 1fr); gap: 2px; }
  .cell { position: relative; overflow: hidden; background: #000; touch-action: pan-y; }
  .cell.zoomed { touch-action: none; }
  .vp { position: relative; transform-origin: center center; will-change: transform; }
  video { display: block; width: 100%; aspect-ratio: 16 / 9; background: #000; object-fit: contain; }
  .stage.single video { max-height: 70vh; }
  .stage.grid .cell.master { outline: 2px solid var(--primary-color); outline-offset: -2px; z-index: 1; }
  .label { position: absolute; left: 6px; bottom: 6px; z-index: 2; padding: 1px 6px; border-radius: 4px;
    font-size: 11px; color: #fff; background: rgba(0,0,0,.5); pointer-events: none; }
  .stage.single .label { display: none; }
  .clock { position: absolute; top: 6px; left: 6px; z-index: 3; padding: 1px 6px; border-radius: 4px;
    background: rgba(0,0,0,.45); color: #fff; font-variant-numeric: tabular-nums; font-size: 12px; pointer-events: none; }
  .stage.noclock .clock { display: none; }

  /* Fullscreen: the stage fills the screen; a control bar hides when idle. */
  .stage:fullscreen { width: 100vw; height: 100vh; display: flex; align-items: center; justify-content: center; }
  .stage.grid:fullscreen { display: grid; grid-template-rows: repeat(var(--rows, 2), 1fr); align-items: stretch; }
  .stage:fullscreen .cell { width: 100%; height: 100%; display: flex; align-items: center; }
  .stage:fullscreen .vp { width: 100%; height: 100%; }
  .stage:fullscreen video { height: 100%; max-height: none; aspect-ratio: auto; }
  .fsbar { display: none; position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%); z-index: 4;
    gap: 6px; align-items: center; padding: 6px 10px; border-radius: 24px; background: rgba(0,0,0,.55);
    transition: opacity .3s; }
  .stage:fullscreen .fsbar { display: flex; }
  .stage.idle .fsbar { opacity: 0; pointer-events: none; }
  .fsbar button { color: #fff; background: rgba(255,255,255,.12); border-color: rgba(255,255,255,.3); }
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
  .veil button { color: #fff; background: rgba(255,255,255,.14); border-color: rgba(255,255,255,.45); }
  .veil:not(.loading) .spin, .veil.loading ha-icon, .veil:not(.error) button { display: none; }
  .stage.grid .veil { gap: 6px; padding: 8px; }
  .stage.grid .spin { width: 28px; height: 28px; }
  .stage.grid .veil ha-icon { --mdc-icon-size: 28px; }
  .stage.grid .vtext { font-size: 13px; }
  .stage.grid .vsub { font-size: 11px; }

  .warn { padding: 6px 12px; font-size: 13px; color: var(--warning-color, #b58100); }
  .warn:empty { display: none; }
  .controls { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px 12px; }
  .spacer { flex: 1; }
  select, input { font: inherit; color: var(--primary-text-color); background: var(--secondary-background-color);
    border: 1px solid var(--divider-color); border-radius: 8px; padding: 4px 6px; min-height: 32px; }
  .tlbar { display: flex; align-items: center; gap: 6px; padding: 0 12px; }
  .range { flex: 1; text-align: center; font-size: 13px; color: var(--secondary-text-color); }
  .track { position: relative; height: 56px; margin: 6px 12px 0; touch-action: none; cursor: pointer;
    background: var(--secondary-background-color); border-radius: 6px; user-select: none; }
  .bars { position: absolute; inset: 0; overflow: hidden; border-radius: 6px; }
  .rec { position: absolute; top: 22px; height: 14px; background: color-mix(in srgb, var(--primary-color) 45%, transparent); }
  .bm { position: absolute; top: 6px; height: 12px; min-width: 3px; border-radius: 2px;
    background: var(--accent-color, #ff9800); }
  .tick { position: absolute; bottom: 0; height: 6px; border-left: 1px solid var(--divider-color); }
  .tick span { position: absolute; bottom: 6px; left: 3px; font-size: 10px; color: var(--secondary-text-color); white-space: nowrap; }
  .nowm { position: absolute; top: 0; bottom: 0; border-left: 2px dashed var(--error-color, #db4437); }
  .ph { position: absolute; top: -4px; bottom: -4px; width: 2px; margin-left: -1px; background: var(--primary-text-color); pointer-events: none; }
  .ph::before { content: ""; position: absolute; top: 0; left: -5px; border: 6px solid transparent; border-top-color: var(--primary-text-color); }
  .hover { position: absolute; top: -24px; transform: translateX(-50%); padding: 1px 6px; border-radius: 4px;
    background: var(--primary-text-color); color: var(--card-background-color, #fff); font-size: 12px; pointer-events: none; white-space: nowrap; }
  .hover[hidden] { display: none; }
  .legend { display: flex; gap: 12px; padding: 4px 12px 0; font-size: 12px; color: var(--secondary-text-color); }
  .legend i { display: inline-block; width: 10px; height: 10px; margin-right: 4px; border-radius: 2px; vertical-align: -1px; }
  /* Event list: bookmarks of every camera, newest first, grouped by day. */
  .events { border-top: 1px solid var(--divider-color); margin-top: 10px; padding: 10px 12px 12px; }
  .ev-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 8px; }
  .ev-title { font-weight: 500; margin-right: 4px; }
  .ev-head button { min-height: 28px; padding: 2px 10px; font-size: 13px; }
  .ev-list { max-height: 420px; overflow-y: auto; }
  .ev-day { position: sticky; top: 0; z-index: 1; padding: 6px 2px 4px; font-size: 12px; font-weight: 500;
    color: var(--secondary-text-color); background: var(--card-background-color, var(--ha-card-background, #fff)); }
  .ev { display: grid; grid-template-columns: auto 1fr auto; align-items: center; column-gap: 10px; row-gap: 2px;
    width: 100%; text-align: left; border-radius: 10px; margin-bottom: 4px; padding: 6px 10px; }
  .ev.on { color: var(--primary-text-color); border-color: var(--primary-color);
    background: color-mix(in srgb, var(--primary-color) 14%, var(--secondary-background-color)); }
  .ev .t { font-variant-numeric: tabular-nums; font-size: 13px; color: var(--secondary-text-color); grid-row: span 2; }
  .ev .n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ev .c { grid-column: 2 / 4; font-size: 12px; color: var(--secondary-text-color); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .ev .cam { font-size: 12px; padding: 1px 8px; border-radius: 10px; white-space: nowrap;
    background: color-mix(in srgb, var(--primary-color) 18%, transparent); }
  .ev-empty { font-size: 13px; color: var(--secondary-text-color); padding: 6px 2px; }
  .jump { display: inline-flex; gap: 4px; }
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
class Player {
  constructor(card, cameraId) {
    this.card = card;
    this.cameraId = cameraId;
    this.hls = null;
    this.session = null; // last vod response for the loaded window
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
      <div class="vp"><video muted playsinline preload="auto"></video><canvas class="still"></canvas></div>
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
  follow(wall, playing, rate, stalled = false) {
    if (this.loading) return;
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
    this._events = []; // bookmarks of all cameras, newest first; drawn: the shown ones
    this._shown = []; // camera ids on screen, in camera order
    this._prevShown = null; // what the grid button goes back to
    this._evRange = EVENT_RANGES[0][0];
    this._evSeq = 0;
    this._drag = false;
    this._onFullscreen = () => this._fullscreenChanged();
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { span: 3600, ...config };
    this._span = Number(this._config.span) || 3600;
    const end = nowS() + this._span * 0.05;
    this._view = { start: end - this._span, end };
    this._showClock = prefs.get("clock", this._config.clock !== false);
  }

  set hass(hass) {
    const first = !this._hass;
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
    else if (this._inited && this._resumeAt != null && this._master) {
      const t = this._resumeAt;
      this._resumeAt = null;
      this._seekAll(t, false);
    }
    this._refresh = setInterval(() => this._periodic(), REFRESH_MS);
    this._syncTimer = setInterval(() => this._sync(), SYNC_MS);
    document.addEventListener("fullscreenchange", this._onFullscreen);
  }

  disconnectedCallback() {
    clearInterval(this._refresh);
    clearInterval(this._syncTimer);
    document.removeEventListener("fullscreenchange", this._onFullscreen);
    clearTimeout(this._idleTimer);
    // Leaving the page ends fullscreen, maybe after our listener is gone.
    this._wasFs = false;
    this._stage?.classList.remove("idle");
    // Whatever state it was in (loading, a gap), pick up there on return.
    if (this._master) this._resumeAt = this._master.wall();
    for (const p of this._players.values()) {
      p.seq++;
      p.loading = false;
      p.destroyMedia();
      p.session = null;
    }
  }

  // ---- setup ------------------------------------------------------------

  async _init() {
    this._inited = true;
    this._render();
    let res;
    try {
      res = await this._ws({ type: "surveillance_station/cameras" });
    } catch (e) {
      this._stageMessage("Can't list cameras", "error", errText(e));
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
    const start = t > 0 ? t : nowS() - LIVE_LAG;
    this._centerOn(start);
    this._loadTimeline();
    this._seekAll(start, t > 0);
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
    root.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="head">
          <div class="chips cams"></div>
        </div>
        <div class="stage single">
          <div class="clock"></div>
          <div class="stage-msg">${VEIL_HTML}</div>
          <div class="fsbar">
            <button class="icon" data-act="play" title="Play / pause"><ha-icon icon="mdi:play"></ha-icon></button>
            <button data-skip="-30">-30s</button>
            <button data-skip="-10">-10s</button>
            <button data-skip="10">+10s</button>
            <button data-skip="30">+30s</button>
            <span class="fsclock"></span>
            <button class="icon" data-act="solo"><ha-icon icon="mdi:view-grid-outline"></ha-icon></button>
            <button class="icon" data-act="fs" title="Exit fullscreen"><ha-icon icon="mdi:fullscreen-exit"></ha-icon></button>
          </div>
        </div>
        <div class="warn"></div>
        <div class="controls">
          <button class="icon" data-act="play" title="Play / pause"><ha-icon icon="mdi:play"></ha-icon></button>
          <button data-skip="-30" title="Back 30 s">-30s</button>
          <button data-skip="-10" title="Back 10 s">-10s</button>
          <button data-skip="10" title="Forward 10 s">+10s</button>
          <button data-skip="30" title="Forward 30 s">+30s</button>
          <select class="speed" title="Playback speed">
            ${SPEEDS.map((s) => `<option value="${s}">${s}×</option>`).join("")}
          </select>
          <button data-act="live" title="Jump to the most recent footage"><ha-icon icon="mdi:access-point"></ha-icon>Latest</button>
          <span class="spacer"></span>
          <span class="jump">
            <input type="datetime-local" step="1" class="when" />
            <button data-act="go">Go</button>
          </span>
          <button class="icon" data-act="solo"><ha-icon icon="mdi:view-grid-outline"></ha-icon></button>
          <button class="icon" data-act="clock" title="Show / hide the time on the video"><ha-icon icon="mdi:clock-outline"></ha-icon></button>
          <button class="icon" data-act="mute" title="Sound"><ha-icon icon="mdi:volume-off"></ha-icon></button>
          <button class="icon" data-act="fs" title="Fullscreen"><ha-icon icon="mdi:fullscreen"></ha-icon></button>
        </div>
        <div class="tlbar">
          <button class="icon" data-act="pan-back" title="Earlier"><ha-icon icon="mdi:chevron-left"></ha-icon></button>
          <div class="chips spans">
            ${SPANS.map(([s, l]) => `<button data-span="${s}">${l}</button>`).join("")}
          </div>
          <div class="range"></div>
          <button class="icon" data-act="pan-fwd" title="Later"><ha-icon icon="mdi:chevron-right"></ha-icon></button>
        </div>
        <div class="track">
          <div class="bars"></div>
          <div class="ph"></div>
          <div class="hover" hidden></div>
        </div>
        <div class="legend">
          <span><i style="background: color-mix(in srgb, var(--primary-color) 45%, transparent)"></i>Recording</span>
          <span><i style="background: var(--accent-color, #ff9800)"></i>Bookmark</span>
        </div>
        <div class="events">
          <div class="ev-head">
            <span class="ev-title">Events</span>
            <span class="spacer"></span>
            ${EVENT_RANGES.map(([s, l]) => `<button data-evrange="${s}">${l}</button>`).join("")}
          </div>
          <div class="ev-list"></div>
        </div>
      </ha-card>`;

    const $ = (s) => root.querySelector(s);
    this._stage = $(".stage");
    this._clock = $(".clock");
    this._fsClock = $(".fsclock");
    this._stageVeil = $(".stage-msg .veil");
    this._track = $(".track");
    this._bars = $(".bars");
    this._ph = $(".ph");
    this._hover = $(".hover");
    this._rangeEl = $(".range");
    this._evList = $(".ev-list");
    this._when = $(".when");

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

    const tr = this._track;
    tr.addEventListener("pointerdown", (e) => {
      tr.setPointerCapture(e.pointerId);
      this._drag = true;
      this._showHover(e);
    });
    tr.addEventListener("pointermove", (e) => this._showHover(e));
    tr.addEventListener("pointerup", (e) => {
      if (!this._drag) return;
      this._drag = false;
      this._hover.hidden = true;
      this._seekAll(this._timeAt(e), true);
    });
    tr.addEventListener("pointercancel", () => {
      this._drag = false;
      this._hover.hidden = true;
    });
    tr.addEventListener("pointerleave", () => {
      if (!this._drag) this._hover.hidden = true;
    });

    this._markSpan();
    this._applyClock();
  }

  _stageMessage(text, kind = "loading", sub = "") {
    setVeil(this._stageVeil, text, kind, sub);
  }

  _renderCameras() {
    const box = this.shadowRoot.querySelector(".cams");
    box.innerHTML = this._cameras
      .map((c) => `<button data-cam="${c.id}"><ha-icon icon="mdi:cctv"></ha-icon>${esc(c.name)}</button>`)
      .join("");
    this._markEventFilters();
    this._loadEvents();
  }

  // ---- players / layout ---------------------------------------------------

  _makePlayer(id) {
    const p = new Player(this, id);
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
    // A follower may have been mid-nudge; the master sets the pace.
    p.video.playbackRate = this._rate;
    this._markCameras();
    this._syncPlayIcon();
    this._syncMuteIcon();
  }

  /** Chips: outlined = shown, filled = the master. */
  _markCameras() {
    for (const b of this.shadowRoot.querySelectorAll("[data-cam]")) {
      const id = Number(b.dataset.cam);
      b.classList.toggle("on", id === this._cameraId);
      b.classList.toggle("shown", this._shown.includes(id));
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
      this._drawEvents();
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
    if (!m) return;
    if (b.dataset.skip) {
      this._seekAll(m.wall() + Number(b.dataset.skip), m.intendsPlay());
      return;
    }
    if (b.dataset.span) {
      this._span = Number(b.dataset.span);
      this._centerOn(m.wall());
      this._markSpan();
      this._loadTimeline();
      return;
    }
    if (b.dataset.ev) {
      const ev = this._events.find((x) => String(x.id) === b.dataset.ev);
      if (ev) this._jumpToEvent(ev);
      return;
    }
    if (b.dataset.evrange) {
      this._evRange = Number(b.dataset.evrange);
      this._markEventFilters();
      this._loadEvents();
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
        const t = nowS() - LIVE_LAG;
        this._centerOn(t);
        this._loadTimeline();
        this._seekAll(t, true);
        break;
      }
      case "go": {
        const t = new Date(this._when.value).getTime() / 1000;
        if (!Number.isFinite(t)) return;
        this._centerOn(t);
        this._loadTimeline();
        this._seekAll(t, true);
        break;
      }
      case "retry": {
        const p = this._players.get(Number(b.closest(".cell")?.dataset.cell));
        const at = p && p !== this._master ? this._master.wall() : p?.target;
        if (p) (p.retry ?? (() => p.load(at, true)))();
        else {
          this._inited = false; // the camera list failed
          this._init();
        }
        break;
      }
      case "mute":
        m.video.muted = !m.video.muted;
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
      // Recordings per camera shown; bookmarks of all cameras in one call
      // (the same query as the event list), filtered to the shown ones.
      const [b, ...rs] = await Promise.all([
        this._ws({ type: "surveillance_station/bookmarks", ...q }),
        ...shown.map((id) => this._ws({ type: "surveillance_station/recordings", camera_id: id, ...q })),
      ]);
      if (seq !== this._tlSeq) return;
      this._recs = rs.flatMap((r) => r.recordings);
      this._bookmarks = b.bookmarks.filter((x) => shown.includes(x.camera_id));
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

    const step = TICK_STEPS.find((s) => span / s <= 8) ?? TICK_STEPS[TICK_STEPS.length - 1];
    const tzo = new Date(start * 1000).getTimezoneOffset() * 60;
    for (let t = Math.ceil((start - tzo) / step) * step + tzo; t <= end; t += step) {
      const label = step >= 3600 && new Date(t * 1000).getHours() === 0 ? fmtDate(t) : fmtTime(t, false);
      html += `<div class="tick" style="left:${x(t)}%"><span>${label}</span></div>`;
    }
    // With several cameras: time where any of them recorded.
    for (const [s, e] of unionOf(this._recs.map((r) => [r.start, r.live ? now : r.end]))) {
      if (e < start || s > end) continue;
      const a = Math.max(x(s), 0);
      const b = Math.min(x(e), 100);
      html += `<div class="rec" style="left:${a}%;width:${Math.max(b - a, 0.1)}%"></div>`;
    }
    for (const bm of this._bookmarks) {
      if (bm.end < start || bm.start > end) continue;
      const a = Math.max(x(bm.start), 0);
      const b = Math.min(x(Math.max(bm.end, bm.start)), 100);
      const cam = this._shown.length > 1 ? `${this._cameraName(bm.camera_id)}: ` : "";
      const tip = `${fmtTime(bm.start)} ${cam}${bm.name}${bm.comment ? " — " + bm.comment : ""}`;
      html += `<div class="bm" style="left:${a}%;width:${Math.max(b - a, 0)}%" title="${esc(tip)}"></div>`;
    }
    if (now >= start && now <= end) html += `<div class="nowm" style="left:${x(now)}%" title="Now"></div>`;
    this._bars.innerHTML = html;

    this._rangeEl.textContent = `${fmtDate(start)} ${fmtTime(start, false)} – ${
      fmtDate(end) === fmtDate(start) ? "" : fmtDate(end) + " "
    }${fmtTime(end, false)}`;
    this._paint(this._currentWall());
  }

  _currentWall() {
    return this._master ? this._master.wall() : nowS() - LIVE_LAG;
  }

  // ---- event list -------------------------------------------------------

  async _loadEvents() {
    const seq = ++this._evSeq;
    const end = nowS();
    let res;
    try {
      res = await this._ws({
        type: "surveillance_station/bookmarks",
        start: Math.floor(end - this._evRange),
        end: Math.ceil(end),
      });
    } catch (e) {
      if (seq === this._evSeq) this._evList.innerHTML = `<div class="ev-empty">Couldn't load events: ${esc(errText(e))}</div>`;
      return;
    }
    if (seq !== this._evSeq) return;
    this._events = res.bookmarks.reverse(); // newest first
    this._drawEvents();
  }

  _markEventFilters() {
    const root = this.shadowRoot;
    for (const b of root.querySelectorAll("[data-evrange]")) {
      b.classList.toggle("on", Number(b.dataset.evrange) === this._evRange);
    }
  }

  _drawEvents() {
    const items = this._events.filter((e) => this._shown.includes(e.camera_id));
    if (!items.length) {
      const label = EVENT_RANGES.find(([s]) => s === this._evRange)?.[1] ?? "";
      const which = this._shown.length === 1 ? this._cameraName(this._shown[0]) : "these cameras";
      this._evList.innerHTML = `<div class="ev-empty">No bookmarks for ${esc(which)} in the last ${label}.</div>`;
      return;
    }
    const today = new Date().toDateString();
    const yesterday = new Date(Date.now() - 86400000).toDateString();
    let html = "";
    let day = null;
    for (const e of items) {
      const d = new Date(e.start * 1000).toDateString();
      if (d !== day) {
        day = d;
        html += `<div class="ev-day">${d === today ? "Today" : d === yesterday ? "Yesterday" : fmtDate(e.start)}</div>`;
      }
      const dur = e.end > e.start ? fmtDur(e.end - e.start) : "";
      html += `<button class="ev" data-ev="${e.id}">
          <span class="t">${fmtTime(e.start)}</span>
          <span class="n">${esc(e.name || "(unnamed)")}${dur ? ` <span class="t">· ${dur}</span>` : ""}</span>
          <span class="cam">${esc(this._cameraName(e.camera_id))}</span>
          ${e.comment ? `<span class="c">${esc(e.comment)}</span>` : ""}
        </button>`;
    }
    this._evList.innerHTML = html;
    this._activeEvent = undefined;
    this._markActiveEvent(this._currentWall());
  }

  /** Highlight the events the playhead is in (on the cameras shown). */
  _markActiveEvent(t) {
    if (!this._evList) return;
    const ids = this._events
      .filter((e) => this._shown.includes(e.camera_id) && t >= e.start - 3 && t <= Math.max(e.end, e.start + 10))
      .map((e) => String(e.id));
    const key = ids.join();
    if (key === this._activeEvent) return;
    this._activeEvent = key;
    for (const b of this._evList.querySelectorAll(".ev")) b.classList.toggle("on", ids.includes(b.dataset.ev));
  }

  _jumpToEvent(ev) {
    this._selectCamera(ev.camera_id, ev.start - 3, true); // a little lead-in
    // On a phone the list is below the fold; bring the video back into view.
    this._stage.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
    if (document.activeElement !== this._when && this.shadowRoot.activeElement !== this._when) {
      this._when.value = toLocalInput(t);
    }
    this._markActiveEvent(t);
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
    this._loadEvents();
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
