/*
 * ss-timeline-card: play back Synology Surveillance Station recordings with a
 * scrubbable wall-clock timeline and the SS bookmarks laid on top of it.
 *
 * Talks to the surveillance_station integration over the HA WebSocket:
 *   surveillance_station/cameras | recordings | bookmarks | vod
 * `vod` returns an HLS playlist URL plus `runs`, the map between playlist time
 * and wall-clock time (a new run starts after every gap in the recordings).
 *
 * Card options (all optional):
 *   camera:   SS camera name or id to start on
 *   span:     timeline width in seconds (default 3600)
 *   entry_id: which Surveillance Station entry, if there is more than one
 * URL parameters override on load: ?ss_camera=<name|id>&ss_time=<epoch seconds>
 */

const CARD_TAG = "ss-timeline-card";
const CARD_VERSION = "0.1.1";
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
const TICK_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];

let hlsPromise;
const loadHls = () => (hlsPromise ??= import(HLS_URL).then((m) => m.default));

const nowS = () => Date.now() / 1000;
const pad = (n) => String(n).padStart(2, "0");
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const errText = (e) => e?.message ?? e?.code ?? String(e);

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

const STYLE = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 12px 12px 8px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  button { font: inherit; color: var(--primary-text-color); background: var(--secondary-background-color);
    border: 1px solid var(--divider-color); border-radius: 16px; padding: 4px 12px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 4px; min-height: 32px; }
  button:hover { border-color: var(--primary-color); }
  button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  button.icon { padding: 4px 8px; border-radius: 50%; min-width: 36px; justify-content: center; }
  ha-icon { --mdc-icon-size: 20px; }
  .wrap { position: relative; background: #000; }
  video { display: block; width: 100%; aspect-ratio: 16 / 9; max-height: 70vh; background: #000; object-fit: contain; }
  .wrap:fullscreen video { max-height: none; height: 100%; }
  .clock { position: absolute; top: 8px; left: 8px; padding: 2px 8px; border-radius: 6px;
    background: rgba(0,0,0,.55); color: #fff; font-variant-numeric: tabular-nums; font-size: 15px; pointer-events: none; }
  .status { position: absolute; left: 50%; bottom: 12px; transform: translateX(-50%); padding: 4px 10px;
    border-radius: 6px; background: rgba(0,0,0,.65); color: #fff; font-size: 13px; pointer-events: none; }
  .status:empty { display: none; }
  .status.err { background: rgba(160,20,20,.85); }
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
  .bmlist { max-height: 220px; overflow-y: auto; padding: 8px 12px 12px; }
  .bmlist .item { display: flex; width: 100%; text-align: left; border-radius: 8px; margin-bottom: 4px; gap: 8px; }
  .bmlist .t { font-variant-numeric: tabular-nums; color: var(--secondary-text-color); }
  .bmlist .n { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bmlist .empty { font-size: 13px; color: var(--secondary-text-color); }
  .jump { display: inline-flex; gap: 4px; }
`;

class SSTimelineCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._cameras = [];
    this._cameraId = null;
    this._recs = [];
    this._bookmarks = [];
    this._session = null; // last vod response for the loaded window
    this._hls = null;
    this._mediaReady = false;
    this._target = nowS() - LIVE_LAG; // wall time we want / are at when nothing is playing
    this._rate = 1;
    this._playSeq = 0;
    this._tlSeq = 0;
    this._followPausedUntil = 0;
    this._drag = false;
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { span: 3600, ...config };
    this._span = Number(this._config.span) || 3600;
    const end = nowS() + this._span * 0.05;
    this._view = { start: end - this._span, end };
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
    else if (this._inited && this._resumeAt != null) {
      const t = this._resumeAt;
      this._resumeAt = null;
      this._seek(t, false);
    }
    this._refresh = setInterval(() => this._periodic(), REFRESH_MS);
  }

  disconnectedCallback() {
    clearInterval(this._refresh);
    clearTimeout(this._nextTimer);
    if (this._session) this._resumeAt = this._currentWall();
    this._playSeq++;
    this._destroyPlayer();
  }

  // ---- setup ------------------------------------------------------------

  async _init() {
    this._inited = true;
    this._render();
    let res;
    try {
      res = await this._ws({ type: "surveillance_station/cameras" });
    } catch (e) {
      this._setStatus(`Can't list cameras: ${errText(e)}`, true);
      return;
    }
    this._cameras = res.cameras.filter((c) => c.enabled);
    if (!this._cameras.length) {
      this._setStatus("No enabled cameras in Surveillance Station", true);
      return;
    }
    const params = new URLSearchParams(location.search);
    const cam = this._findCamera(params.get("ss_camera") ?? this._config.camera) ?? this._cameras[0];
    const t = Number(params.get("ss_time"));
    this._renderCameras();
    this._selectCamera(cam.id, t > 0 ? t : nowS() - LIVE_LAG, t > 0);
  }

  _findCamera(want) {
    if (want == null || want === "") return null;
    const w = String(want).toLowerCase();
    return this._cameras.find((c) => String(c.id) === w || c.name.toLowerCase() === w) ?? null;
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
        <div class="wrap">
          <video muted playsinline preload="auto"></video>
          <div class="clock"></div>
          <div class="status"></div>
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
        <div class="bmlist"></div>
      </ha-card>`;

    const $ = (s) => root.querySelector(s);
    this._video = $("video");
    this._wrap = $(".wrap");
    this._clock = $(".clock");
    this._status = $(".status");
    this._track = $(".track");
    this._bars = $(".bars");
    this._ph = $(".ph");
    this._hover = $(".hover");
    this._rangeEl = $(".range");
    this._bmList = $(".bmlist");
    this._when = $(".when");
    this._playIcon = $('[data-act="play"] ha-icon');
    this._muteIcon = $('[data-act="mute"] ha-icon');

    if (hevcSupport() === false) {
      $(".warn").textContent =
        "This browser reports no H.265 (HEVC) support; playback of Surveillance Station recordings will likely fail here.";
    }

    root.querySelector("ha-card").addEventListener("click", (e) => this._onClick(e));
    $(".speed").addEventListener("change", (e) => {
      this._rate = Number(e.target.value);
      this._video.playbackRate = this._video.defaultPlaybackRate = this._rate;
    });

    const v = this._video;
    v.addEventListener("timeupdate", () => this._onTime());
    v.addEventListener("seeked", () => this._onTime());
    v.addEventListener("loadeddata", () => {
      this._mediaReady = true;
      this._onTime();
    });
    v.addEventListener("waiting", () => this._setStatus("Buffering…"));
    v.addEventListener("playing", () => this._setStatus(""));
    v.addEventListener("play", () => this._playIcon.setAttribute("icon", "mdi:pause"));
    v.addEventListener("pause", () => this._playIcon.setAttribute("icon", "mdi:play"));
    v.addEventListener("ended", () => this._continue());
    v.addEventListener("volumechange", () =>
      this._muteIcon.setAttribute("icon", v.muted ? "mdi:volume-off" : "mdi:volume-high")
    );

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
      this._seek(this._timeAt(e), true);
    });
    tr.addEventListener("pointercancel", () => {
      this._drag = false;
      this._hover.hidden = true;
    });
    tr.addEventListener("pointerleave", () => {
      if (!this._drag) this._hover.hidden = true;
    });

    this._markSpan();
  }

  _renderCameras() {
    const box = this.shadowRoot.querySelector(".cams");
    box.innerHTML = this._cameras
      .map((c) => `<button data-cam="${c.id}"><ha-icon icon="mdi:cctv"></ha-icon>${esc(c.name)}</button>`)
      .join("");
  }

  _onClick(e) {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.cam) {
      const wall = this._session ? this._currentWall() : this._target;
      this._selectCamera(Number(b.dataset.cam), wall, !this._video.paused || !this._session);
      return;
    }
    if (b.dataset.skip) {
      this._seek(this._currentWall() + Number(b.dataset.skip), !this._video.paused);
      return;
    }
    if (b.dataset.span) {
      this._span = Number(b.dataset.span);
      this._centerOn(this._currentWall());
      this._markSpan();
      this._loadTimeline();
      return;
    }
    if (b.dataset.bm) {
      const t = Number(b.dataset.bm);
      this._centerOn(t);
      this._loadTimeline();
      this._seek(t, true);
      return;
    }
    switch (b.dataset.act) {
      case "play":
        if (!this._session) this._seek(this._target, true);
        else if (this._video.paused) this._video.play().catch(() => {});
        else this._video.pause();
        break;
      case "live": {
        const t = nowS() - LIVE_LAG;
        this._centerOn(t);
        this._loadTimeline();
        this._seek(t, true);
        break;
      }
      case "go": {
        const t = new Date(this._when.value).getTime() / 1000;
        if (!Number.isFinite(t)) return;
        this._centerOn(t);
        this._loadTimeline();
        this._seek(t, true);
        break;
      }
      case "mute":
        this._video.muted = !this._video.muted;
        break;
      case "fs":
        if (document.fullscreenElement) document.exitFullscreen();
        else if (this._wrap.requestFullscreen) this._wrap.requestFullscreen().catch(() => {});
        else this._video.webkitEnterFullscreen?.();
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

  _selectCamera(id, t, autoplay) {
    this._cameraId = id;
    for (const b of this.shadowRoot.querySelectorAll("[data-cam]")) {
      b.classList.toggle("on", Number(b.dataset.cam) === id);
    }
    this._recs = [];
    this._bookmarks = [];
    this._centerOn(t);
    this._loadTimeline();
    this._loadWindow(t, autoplay);
  }

  // ---- time mapping -----------------------------------------------------

  /** Playlist position for a wall time; inside a gap, the start of the next run. */
  _wallToMedia(t) {
    for (const r of this._session?.runs ?? []) {
      if (t < r.wall_start) return r.media_start;
      if (t < r.wall_start + r.duration) return r.media_start + (t - r.wall_start);
    }
    return null;
  }

  _mediaToWall(m) {
    const runs = this._session?.runs ?? [];
    for (let i = 0; i < runs.length; i++) {
      const r = runs[i];
      if (m < r.media_start + r.duration || i === runs.length - 1) {
        return r.wall_start + Math.max(0, m - r.media_start);
      }
    }
    return null;
  }

  _currentWall() {
    if (this._session && this._mediaReady) {
      return this._mediaToWall(this._video.currentTime) ?? this._target;
    }
    return this._target;
  }

  // ---- playback ---------------------------------------------------------

  _destroyPlayer() {
    if (this._hls) {
      this._hls.destroy();
      this._hls = null;
    }
    this._mediaReady = false;
    const v = this._video;
    if (v) {
      v.pause();
      v.removeAttribute("src");
      v.load();
    }
  }

  _seek(t, autoplay) {
    t = Math.min(t, nowS() - LIVE_LAG);
    const s = this._session;
    if (s && this._mediaReady && t >= s.start && t < s.end - 1) {
      const m = this._wallToMedia(t);
      if (m != null) {
        this._target = t;
        this._video.currentTime = m;
        this._paint(t);
        if (autoplay) this._video.play().catch(() => {});
        return;
      }
    }
    this._loadWindow(t, autoplay);
  }

  async _loadWindow(t, autoplay) {
    const seq = ++this._playSeq;
    clearTimeout(this._nextTimer);
    const now = nowS();
    t = Math.min(t, now - LIVE_LAG);
    this._target = t;
    this._paint(t);
    this._setStatus("Loading…");
    let res;
    try {
      res = await this._ws({
        type: "surveillance_station/vod",
        camera_id: this._cameraId,
        start: t - PRE_ROLL,
        end: Math.min(t + WINDOW_AHEAD, now),
      });
    } catch (e) {
      if (seq === this._playSeq) this._setStatus(`Playback failed: ${errText(e)}`, true);
      return;
    }
    if (seq !== this._playSeq) return;
    if (!res.url) {
      this._destroyPlayer();
      this._session = null;
      this._setStatus("No recording at this time", true);
      return;
    }
    const Hls = await loadHls();
    if (seq !== this._playSeq) return;
    this._destroyPlayer();
    this._session = res;
    const start = this._wallToMedia(t) ?? 0;
    if (start > 0 && t < res.runs[0].wall_start - 1) {
      this._target = res.runs[0].wall_start;
    }
    const v = this._video;
    v.playbackRate = v.defaultPlaybackRate = this._rate;

    if (!Hls.isSupported()) {
      if (v.canPlayType("application/vnd.apple.mpegurl")) {
        // Safari without MSE: native HLS.
        v.src = res.url;
        v.addEventListener(
          "loadedmetadata",
          () => {
            v.currentTime = start;
            if (autoplay) v.play().catch(() => {});
          },
          { once: true }
        );
      } else {
        this._setStatus("This browser can't play HLS video", true);
      }
      return;
    }

    const hls = new Hls({
      startPosition: start,
      maxBufferLength: 30,
      maxMaxBufferLength: 60,
      backBufferLength: 90,
    });
    this._hls = hls;
    let recovered = false;
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (autoplay) v.play().catch(() => {});
      else this._setStatus("");
    });
    hls.on(Hls.Events.ERROR, (_, d) => {
      if (!d.fatal || hls !== this._hls) return;
      if (d.type === Hls.ErrorTypes.MEDIA_ERROR && !recovered) {
        recovered = true;
        hls.recoverMediaError();
        return;
      }
      const codec = /codec/i.test(d.details) || d.details === "manifestIncompatibleCodecsError";
      this._setStatus(
        codec ? "This browser can't decode H.265 (HEVC) video" : `Playback error: ${d.details}`,
        true
      );
      hls.destroy();
      if (this._hls === hls) this._hls = null;
    });
    hls.loadSource(res.url);
    hls.attachMedia(v);
  }

  /** At the end of a window, carry on into whatever was recorded next. */
  _continue() {
    const s = this._session;
    if (!s) return;
    const next = s.end;
    const ready = nowS() - LIVE_LAG - 10;
    if (ready >= next) {
      this._loadWindow(next, true);
    } else {
      this._setStatus("Waiting for new footage…");
      this._nextTimer = setTimeout(() => this._continue(), (next - ready) * 1000 + 500);
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
    if (this._cameraId == null) return;
    const q = { camera_id: this._cameraId, start: Math.floor(start), end: Math.ceil(end) };
    try {
      const [r, b] = await Promise.all([
        this._ws({ type: "surveillance_station/recordings", ...q }),
        this._ws({ type: "surveillance_station/bookmarks", ...q }),
      ]);
      if (seq !== this._tlSeq) return;
      this._recs = r.recordings;
      this._bookmarks = b.bookmarks;
    } catch (e) {
      if (seq === this._tlSeq) this._setStatus(`Timeline failed: ${errText(e)}`, true);
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
    for (const r of this._recs) {
      const e = r.live ? now : r.end;
      if (e < start || r.start > end) continue;
      const a = Math.max(x(r.start), 0);
      const b = Math.min(x(e), 100);
      html += `<div class="rec" style="left:${a}%;width:${Math.max(b - a, 0.1)}%"></div>`;
    }
    for (const bm of this._bookmarks) {
      if (bm.end < start || bm.start > end) continue;
      const a = Math.max(x(bm.start), 0);
      const b = Math.min(x(Math.max(bm.end, bm.start)), 100);
      const tip = `${fmtTime(bm.start)} ${bm.name}${bm.comment ? " — " + bm.comment : ""}`;
      html += `<div class="bm" style="left:${a}%;width:${Math.max(b - a, 0)}%" title="${esc(tip)}"></div>`;
    }
    if (now >= start && now <= end) html += `<div class="nowm" style="left:${x(now)}%" title="Now"></div>`;
    this._bars.innerHTML = html;

    this._rangeEl.textContent = `${fmtDate(start)} ${fmtTime(start, false)} – ${
      fmtDate(end) === fmtDate(start) ? "" : fmtDate(end) + " "
    }${fmtTime(end, false)}`;
    this._drawBookmarkList();
    this._paint(this._currentWall());
  }

  _drawBookmarkList() {
    const { start, end } = this._view;
    const items = this._bookmarks.filter((b) => b.end >= start && b.start <= end).reverse();
    if (!items.length) {
      this._bmList.innerHTML = `<div class="empty">No bookmarks in this range.</div>`;
      return;
    }
    this._bmList.innerHTML = items
      .map((b) => {
        const dur = b.end > b.start ? ` · ${fmtDur(b.end - b.start)}` : "";
        return `<button class="item" data-bm="${b.start - 3}" title="${esc(b.comment)}">
          <span class="t">${fmtDate(b.start)} ${fmtTime(b.start)}</span>
          <span class="n">${esc(b.name || "(unnamed)")}</span>
          <span class="t">${dur}</span></button>`;
      })
      .join("");
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
    this._clock.textContent = `${fmtDate(t)} · ${fmtTime(t)}`;
    const { start, end } = this._view;
    this._ph.style.display = t >= start && t <= end ? "" : "none";
    this._ph.style.left = `${((t - start) / (end - start)) * 100}%`;
    if (document.activeElement !== this._when && this.shadowRoot.activeElement !== this._when) {
      this._when.value = toLocalInput(t);
    }
  }

  _onTime() {
    if (!this._session || !this._mediaReady) return;
    const t = this._currentWall();
    this._target = t;
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
  }

  _setStatus(text, error = false) {
    if (!this._status) return;
    this._status.textContent = text;
    this._status.classList.toggle("err", !!error);
  }
}

// Loaded through add_extra_js_url, this module races HA's own app bundle,
// which swaps window.customElements for a scoped-registry polyfill. A
// definition made before the swap lands in the old registry and the frontend
// reports "Custom element doesn't exist". So wait for the frontend's root
// element, then define on whatever registry is current.
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
