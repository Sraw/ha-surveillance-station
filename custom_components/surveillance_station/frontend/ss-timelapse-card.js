/*
 * ss-timelapse-card: one camera's Surveillance Station time-lapse, a day at a time.
 *
 * Loaded by ss-timeline-card.js (same version), so it needs no resource of
 * its own. Talks to the surveillance_station integration over the HA WebSocket:
 *   surveillance_station/timelapse_days  cameras with a time-lapse task, and their days
 *   surveillance_station/timelapse       a playback session for one camera and day
 * A day (NAS-local midnight to midnight) is the stretches of the task's files
 * that fall on it: SS rolls files over whenever the task started, not at
 * midnight. The segments are SS's 4K time-lapse transcoded by HA (H.265 where
 * the browser plays it, else H.264), fetched and fed to MSE here; without MSE
 * the session's HLS playlist goes to the <video> itself. `runs` maps playlist
 * time to wall time (each run is one file, `rate` x real time).
 *
 * Card options (all optional):
 *   camera:   name or id to start on (default: the last one viewed, else the first)
 *   entry_id: which Surveillance Station entry, if there is more than one
 */

const TL_TAG = "ss-timelapse-card";
const TL_MSE = window.MediaSource ?? window.ManagedMediaSource;
const AHEAD = 18; // seconds of video fetched ahead of the playhead
const BEHIND = 30; // seconds of played video kept (a short step back is instant)
const PARALLEL = 3; // segments fetched at once (the server transcodes as many)
const HEVC_PROBE = 'video/mp4; codecs="hvc1.1.6.L93.B0"';
// As the timeline card's: without a poster, Android WebView (the HA app)
// paints a big grey play button over a video with no frame yet.
const TL_BLANK_POSTER = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
const TL_LOADING_DELAY_MS = 400; // a quick load shows no veil at all
// Timeline zoom: how much of the day the scrub bar shows (wall seconds).
const TL_SPANS = [[86400, "24h"], [6 * 3600, "6h"], [3600, "1h"], [900, "15m"]];
// Skip buttons, in seconds of the time-lapse itself (what is watched).
const TL_SKIPS = [-30, -10, 10, 30];
const TL_TICK_STEPS = [300, 900, 1800, 3600, 3 * 3600, 6 * 3600];
const TL_FOLLOW_PAUSE_MS = 15_000; // after a manual pan, the view doesn't follow the playhead

function tlPrefs(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(`ss-timelapse:${key}`));
    localStorage.setItem(`ss-timelapse:${key}`, JSON.stringify(value));
  } catch (e) {
    return null;
  }
  return null;
}

function tlFindBox(buf, tag) {
  const t = [...tag].map((c) => c.charCodeAt(0));
  for (let i = 4; i + 4 <= buf.length; i++)
    if (buf[i] === t[0] && buf[i + 1] === t[1] && buf[i + 2] === t[2] && buf[i + 3] === t[3]) return i;
  return -1;
}

/** RFC 6381 codec string of an init segment (hvcC / avcC). */
function tlCodecOf(moov) {
  const hex = (n) => n.toString(16).toUpperCase();
  let i = tlFindBox(moov, "hvcC");
  if (i > 0) {
    const c = moov.subarray(i + 4);
    const space = ["", "A", "B", "C"][c[1] >> 6];
    const tier = c[1] & 0x20 ? "H" : "L";
    const compat = ((c[2] << 24) | (c[3] << 16) | (c[4] << 8) | c[5]) >>> 0;
    let rev = 0; // the flags are written bit-reversed
    for (let k = 0; k < 32; k++) rev = (rev << 1) | ((compat >>> k) & 1);
    const cons = [...c.subarray(6, 12)];
    while (cons.length && !cons.at(-1)) cons.pop();
    return ["hvc1", `${space}${c[1] & 0x1f}`, hex(rev >>> 0), `${tier}${c[12]}`, ...cons.map(hex)].join(".");
  }
  i = tlFindBox(moov, "avcC");
  if (i > 0) {
    const c = moov.subarray(i + 4);
    return "avc1." + [c[1], c[2], c[3]].map((x) => x.toString(16).padStart(2, "0")).join("");
  }
  return null;
}

/**
 * Plays one session's segments through MSE: keeps AHEAD seconds fetched past
 * the playhead (PARALLEL at a time, appended in order), drops what's more
 * than BEHIND seconds played, and starts over where a seek lands outside
 * what's buffered. Segment i starts at media time starts[i]. The init
 * segment is fetched once per output size (maps: each file's first segment
 * and its size; the same size transcodes to the same init).
 */
class SegmentFeed {
  constructor(video, session, { onError, onWaiting }) {
    this.video = video;
    this.base = session.url.replace(/index\.m3u8$/, "");
    this.starts = session.segments;
    this.maps = session.maps;
    this.duration = session.duration;
    this.onError = onError;
    this.onWaiting = onWaiting;
    this.appended = new Set(); // segments in the buffer (dropped as trimmed)
    this.fetches = new Map(); // index -> {promise: Promise<Uint8Array>, abort: AbortController}
    this.sizeDone = null; // the output size whose init is in the buffer
    this.gen = 0;
    this.closed = false;
    this.ms = new TL_MSE();
    if (TL_MSE !== window.MediaSource) video.disableRemotePlayback = true;
    this.url = URL.createObjectURL(this.ms);
    video.src = this.url;
    this.ms.addEventListener("sourceopen", () => this.pump(), { once: true });
    this._tick = () => this.pump();
    video.addEventListener("timeupdate", this._tick);
    video.addEventListener("seeking", this._onSeek = () => this.seeked());
    video.addEventListener("waiting", this._tick);
  }

  close() {
    this.closed = true;
    this.gen++;
    for (const f of this.fetches.values()) f.abort.abort();
    this.fetches.clear();
    this.initAbort?.abort();
    this.video.removeEventListener("timeupdate", this._tick);
    this.video.removeEventListener("seeking", this._onSeek);
    this.video.removeEventListener("waiting", this._tick);
    this.video.removeAttribute("src");
    this.video.load();
    URL.revokeObjectURL(this.url);
  }

  indexAt(t) {
    let lo = 0;
    let hi = this.starts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.starts[mid] <= t + 1e-3) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }

  mapOf(i) {
    let m = this.maps[0];
    for (const k of this.maps) if (k.index <= i) m = k;
    return m;
  }

  bufferedEnd(t) {
    const b = this.video.buffered;
    for (let k = 0; k < b.length; k++) if (b.start(k) <= t + 0.1 && t < b.end(k)) return b.end(k);
    return null;
  }

  seeked() {
    // Whatever the pump was waiting for may not be wanted any more: it starts
    // over from what the new position needs (see pump), and the fetches
    // outside that are dropped, so HA doesn't do them before the wanted ones.
    const t = this.video.currentTime;
    const end = this.bufferedEnd(t);
    const at = end === null ? this.indexAt(t) : this.indexFrom(end);
    this.gen++;
    this.initAbort?.abort();
    for (const [k, f] of [...this.fetches]) {
      if (k < at || k >= at + PARALLEL) {
        f.abort.abort();
        this.fetches.delete(k);
      }
    }
    if (end === null) this.onWaiting?.();
    this.pump();
  }

  get(url, signal) {
    return fetch(url, { credentials: "same-origin", signal }).then((r) => {
      if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
      return r.arrayBuffer().then((b) => new Uint8Array(b));
    });
  }

  segment(i) {
    if (!this.fetches.has(i)) {
      const abort = new AbortController();
      const promise = this.get(`${this.base}seg/${i}.m4s`, abort.signal);
      const f = { promise, abort };
      promise.catch(() => this.fetches.get(i) === f && this.fetches.delete(i));
      this.fetches.set(i, f);
    }
    return this.fetches.get(i).promise;
  }

  async append(data) {
    const sb = this.sb;
    if (sb.updating) await new Promise((r) => sb.addEventListener("updateend", r, { once: true }));
    sb.appendBuffer(data);
    await new Promise((resolve, reject) => {
      sb.addEventListener("updateend", resolve, { once: true });
      sb.addEventListener("error", reject, { once: true });
    });
  }

  async trim() {
    const t = this.video.currentTime;
    const b = this.video.buffered;
    if (!this.sb || this.sb.updating || !b.length || b.start(0) >= t - BEHIND - 10) return;
    const cut = Math.max(0, t - BEHIND);
    this.sb.remove(0, cut);
    await new Promise((r) => this.sb.addEventListener("updateend", r, { once: true }));
    for (const k of [...this.appended]) if ((this.starts[k + 1] ?? this.duration) <= cut) this.appended.delete(k);
  }

  /** The first segment starting at or after media time t (starts.length: none). */
  indexFrom(t) {
    const i = this.indexAt(t);
    return this.starts[i] >= t - 0.05 ? i : i + 1;
  }

  /** Play on over a hole in the buffer (a segment that came out a little short). */
  skipGap(t) {
    const b = this.video.buffered;
    for (let k = 0; k < b.length; k++) {
      if (b.start(k) > t && b.start(k) - t < 1) {
        this.video.currentTime = b.start(k) + 0.01;
        return true;
      }
    }
    return false;
  }

  async pump() {
    // "ended" too: an append (after a seek back) opens it again.
    if (this.closed || this.busy || this.ms.readyState === "closed") return;
    this.busy = true;
    try {
      for (;;) {
        if (this.closed || this.ms.readyState === "closed") break;
        const gen = this.gen;
        const t = this.video.currentTime;
        const end = this.bufferedEnd(t);
        // What comes next follows from the buffer at the playhead alone: the
        // segment where its buffered stretch ends, or with nothing buffered
        // there, the playhead's own (after any seek, into buffered or not).
        let i = end === null ? this.indexAt(t) : this.indexFrom(end);
        if (end === null && this.appended.has(i)) {
          // Appended, yet not buffered at t: a hole, not something to fetch again.
          if (this.skipGap(t)) break;
          i += 1;
        }
        if (i >= this.starts.length) {
          if (!this.sb?.updating && this.ms.readyState === "open" && end !== null) this.ms.endOfStream();
          break;
        }
        if (this.starts[i] > (end ?? t) + AHEAD) break;
        // Nothing to play yet (opened, or a seek): that one segment alone, so
        // it gets the whole link from the NAS; then PARALLEL ahead.
        const n = end === null ? 1 : PARALLEL;
        for (let k = i; k < Math.min(this.starts.length, i + n); k++) this.segment(k);
        const map = this.mapOf(i);
        let init = null;
        if (map.size !== this.sizeDone) {
          this.initAbort = new AbortController();
          init = await this.get(`${this.base}init/${map.index}.mp4`, this.initAbort.signal);
          if (gen !== this.gen || this.closed) continue;
        }
        let media;
        try {
          media = await this.segment(i);
        } catch (e) {
          if (gen !== this.gen || this.closed) continue; // aborted by a seek
          if (this.retried !== i) {
            // Once more, after a moment (a NAS hiccup), before giving up.
            this.retried = i;
            await new Promise((r) => setTimeout(r, 1500));
            continue;
          }
          throw e;
        }
        if (gen !== this.gen || this.closed) continue;
        if (init) {
          if (!this.sb) {
            const codec = tlCodecOf(init);
            const mime = `video/mp4; codecs="${codec}"`;
            if (!codec || !TL_MSE.isTypeSupported(mime))
              throw Object.assign(new Error(`this browser can't play ${codec}`), { codec });
            this.ms.duration = this.duration;
            this.sb = this.ms.addSourceBuffer(mime);
          }
          await this.append(init);
          this.sizeDone = map.size;
        }
        await this.trim();
        await this.append(media);
        this.fetches.delete(i);
        this.appended.add(i);
      }
    } catch (e) {
      if (!this.closed) this.onError?.(e);
    } finally {
      this.busy = false;
    }
  }
}

const TL_STYLE = `
  :host { display: block; }
  ha-card { overflow: hidden; container-type: inline-size; }
  ha-icon { --mdc-icon-size: 20px; }
  .bar { display: flex; gap: 6px; padding: 8px 12px 0; align-items: center; }
  .scroll { overflow-x: auto; scrollbar-width: none; flex: 1; display: flex; gap: 6px; }
  .scroll::-webkit-scrollbar { display: none; }
  button { font: inherit; font-size: 13px; color: var(--primary-text-color); background: transparent;
    border: 1px solid var(--divider-color); border-radius: 16px; padding: 4px 10px; cursor: pointer;
    white-space: nowrap; flex: none; }
  button.on { background: color-mix(in srgb, var(--primary-color) 22%, transparent); border-color: var(--primary-color); }
  button.icon { border: none; border-radius: 50%; padding: 6px; display: inline-flex; }
  button:disabled { opacity: .4; cursor: default; }
  button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  .day small { color: var(--secondary-text-color); margin-right: 4px; }
  .stage { position: relative; background: #000; aspect-ratio: 16 / 9; margin: 8px auto 0; max-width: 100%; }
  video { width: 100%; height: 100%; display: block; object-fit: contain; }
  .clock { position: absolute; left: 8px; top: 8px; color: #fff; background: rgba(0,0,0,.45);
    padding: 2px 8px; border-radius: 4px; font-size: 14px; font-variant-numeric: tabular-nums; pointer-events: none; }
  /* The veil, as the timeline card's: spinner (loading) or icon, a line, a sub-line. */
  .veil { position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 10px; padding: 16px; text-align: center; color: #fff;
    background: rgba(0,0,0,.3); backdrop-filter: blur(18px) saturate(1.15); -webkit-backdrop-filter: blur(18px) saturate(1.15);
    opacity: 1; visibility: visible; transition: opacity .25s, visibility 0s; }
  .veil.off { opacity: 0; visibility: hidden; pointer-events: none; transition: opacity .25s, visibility 0s .25s; }
  .spin { width: 44px; height: 44px; border-radius: 50%; border: 3px solid rgba(255,255,255,.25);
    border-top-color: #fff; animation: tl-spin .9s linear infinite; }
  @keyframes tl-spin { to { transform: rotate(360deg); } }
  .veil ha-icon { --mdc-icon-size: 42px; opacity: .9; }
  .veil.error ha-icon { color: #ff8a80; }
  .vtext { font-size: 16px; font-weight: 500; text-shadow: 0 1px 4px rgba(0,0,0,.6); max-width: 90%; }
  .vsub { font-size: 13px; opacity: .85; font-variant-numeric: tabular-nums; text-shadow: 0 1px 3px rgba(0,0,0,.6); }
  .vsub:empty { display: none; }
  .veil:not(.loading) .spin, .veil.loading ha-icon { display: none; }
  .controls { display: flex; align-items: center; gap: 2px; padding: 6px 8px 0; }
  .spacer { flex: 1; }
  .range { flex: 1; min-width: 0; font-size: 12px; color: var(--secondary-text-color); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; text-align: center; font-variant-numeric: tabular-nums; }
  .spans { display: flex; border: 1px solid var(--divider-color); border-radius: 16px; overflow: hidden; flex: none; }
  .spans button { border: none; border-radius: 0; padding: 4px 9px; font-size: 12px; color: var(--secondary-text-color); }
  .spans button + button { border-left: 1px solid var(--divider-color); }
  .spans button.on { color: var(--primary-text-color); font-weight: 600;
    background: color-mix(in srgb, var(--primary-color) 22%, transparent); }
  .tlrow { display: flex; align-items: flex-start; gap: 2px; padding: 4px 4px 10px; }
  .tlrow button.icon { padding: 3px; margin-top: 2px; }
  @container (max-width: 520px) {
    .controls .wide, .range { display: none; }
    .controls .spacer { display: block; }
    .spans button { padding: 4px 7px; }
  }
  .scrub { position: relative; flex: 1; height: 34px; cursor: pointer; touch-action: none; }
  .track { position: absolute; left: 0; right: 0; top: 6px; height: 10px; border-radius: 5px;
    background: color-mix(in srgb, var(--secondary-text-color) 18%, transparent); overflow: hidden; }
  .cov { position: absolute; top: 0; bottom: 0; background: color-mix(in srgb, var(--primary-color) 45%, transparent); }
  .head { position: absolute; top: 2px; width: 2px; height: 18px; margin-left: -1px; background: var(--primary-color); }
  .ticks { position: absolute; left: 0; right: 0; top: 19px; height: 14px; font-size: 10px; color: var(--secondary-text-color); }
  .ticks span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
  .ticks span.l { transform: none; }
  .ticks span.r { transform: translateX(-100%); }
`;

class SSTimelapseCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._index = null; // timelapse_days' answer
    this._cameraId = null;
    this._date = null;
    this._session = null;
    this._feed = null;
    this._seq = 0;
    const span = Number(tlPrefs("span"));
    this._span = TL_SPANS.some(([v]) => v === span) ? span : TL_SPANS[0][0];
    this._view = null; // {start, end}: the part of the day the scrub bar shows
    this._followPausedUntil = 0;
    this._onResize = () => this._fit();
  }

  setConfig(config) {
    this._config = { ...config };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this.isConnected) this._init();
  }

  getCardSize() {
    return 8;
  }

  connectedCallback() {
    if (this._hass && !this._inited) this._init();
    window.addEventListener("resize", this._onResize);
  }

  disconnectedCallback() {
    // A hidden card holds no stream open, and a call still under way when it
    // went opens none: the next visit starts the day again.
    this._seq++;
    this._closeFeed();
    this._inited = false;
    window.removeEventListener("resize", this._onResize);
    this._resizeObs?.disconnect();
  }

  _ws(msg) {
    const extra = this._config.entry_id ? { entry_id: this._config.entry_id } : {};
    return this._hass.connection.sendMessagePromise({ ...extra, ...msg });
  }

  _fmt(ts, opts) {
    try {
      return new Intl.DateTimeFormat(this._hass?.locale?.language || undefined, {
        timeZone: this._index?.timezone, ...opts,
      }).format(new Date(ts * 1000));
    } catch (e) {
      return new Date(ts * 1000).toLocaleString();
    }
  }

  async _init() {
    this._inited = true;
    this._render();
    this._message("Loading");
    const seq = ++this._seq;
    let index;
    try {
      index = await this._ws({ type: "surveillance_station/timelapse_days" });
    } catch (e) {
      if (seq === this._seq) this._message("Couldn't list the time-lapse", "error", e.message || e.code || String(e));
      return;
    }
    if (seq !== this._seq || !this.isConnected) return;
    this._index = index;
    const cams = this._index.cameras;
    if (!cams.length) {
      this._message("No time-lapse tasks in Surveillance Station", "empty");
      return;
    }
    const want = this._config.camera ?? tlPrefs("camera");
    const cam = cams.find((c) => String(c.id) === String(want) || c.name === want) ?? cams[0];
    this._renderCameras();
    this._selectCamera(cam.id);
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>${TL_STYLE}</style>
      <ha-card>
        <div class="bar"><div class="scroll cams"></div></div>
        <div class="bar">
          <button class="icon prev" title="Previous day"><ha-icon icon="mdi:chevron-left"></ha-icon></button>
          <div class="scroll days"></div>
          <button class="icon next" title="Next day"><ha-icon icon="mdi:chevron-right"></ha-icon></button>
        </div>
        <div class="stage">
          <video muted playsinline disablepictureinpicture poster="${TL_BLANK_POSTER}"></video>
          <div class="clock"></div>
          <div class="veil off"><div class="spin"></div><ha-icon></ha-icon><div class="vtext"></div><div class="vsub"></div></div>
        </div>
        <div class="controls">
          <button class="icon play" title="Play"><ha-icon icon="mdi:play"></ha-icon></button>
          ${TL_SKIPS.map((d) => `<button class="icon skip${Math.abs(d) >= 30 ? " wide" : ""}" data-skip="${d}" title="${d < 0 ? "Back" : "Forward"} ${Math.abs(d)} s"><ha-icon icon="mdi:${d < 0 ? "rewind" : "fast-forward"}-${Math.abs(d)}"></ha-icon></button>`).join("")}
          <span class="range"></span><span class="spacer" hidden></span>
          <div class="spans">${TL_SPANS.map(([v, l]) => `<button data-span="${v}">${l}</button>`).join("")}</div>
          <button class="icon full" title="Full screen"><ha-icon icon="mdi:fullscreen"></ha-icon></button>
        </div>
        <div class="tlrow">
          <button class="icon pan" data-pan="-1" title="Earlier"><ha-icon icon="mdi:chevron-left"></ha-icon></button>
          <div class="scrub"><div class="track"></div><div class="head"></div><div class="ticks"></div></div>
          <button class="icon pan" data-pan="1" title="Later"><ha-icon icon="mdi:chevron-right"></ha-icon></button>
        </div>
      </ha-card>`;
    const $ = (s) => this.shadowRoot.querySelector(s);
    this._dragging = false;
    this._video = $("video");
    this._video.muted = true;
    this._clock = $(".clock");
    this._veil = $(".veil");
    this._scrub = $(".scrub");
    this._headEl = $(".head");
    this._playBtn = $(".play");
    $(".prev").addEventListener("click", () => this._stepDay(1));
    $(".next").addEventListener("click", () => this._stepDay(-1));
    this._playBtn.addEventListener("click", () => {
      if (this._resume) {
        const { at } = this._resume;
        this._resume = null;
        this._selectDay(this._date, at, true);
      } else if (this._video.paused) this._play();
      else this._video.pause();
    });
    for (const b of this.shadowRoot.querySelectorAll("[data-skip]")) b.addEventListener("click", () => this._skip(Number(b.dataset.skip)));
    for (const b of this.shadowRoot.querySelectorAll("[data-span]")) b.addEventListener("click", () => this._zoom(Number(b.dataset.span)));
    for (const b of this.shadowRoot.querySelectorAll("[data-pan]")) b.addEventListener("click", () => this._pan(Number(b.dataset.pan)));
    this._markSpan();
    $(".full").addEventListener("click", () => {
      const stage = $(".stage");
      if (document.fullscreenElement) document.exitFullscreen();
      else (stage.requestFullscreen ?? this._video.webkitEnterFullscreen)?.call(stage.requestFullscreen ? stage : this._video);
    });
    const v = this._video;
    for (const ev of ["play", "pause"]) v.addEventListener(ev, () => this._syncPlay());
    v.addEventListener("timeupdate", () => this._paint());
    v.addEventListener("playing", () => {
      this._played = true;
      this._message(null);
    });
    // Until the first frame the veil says Loading (and which day); Buffering is for a stall after.
    v.addEventListener("waiting", () => this._played && this._stall("Buffering"));
    // Paused: a frame is on screen once it can play / the seek landed (also when autoplay was refused).
    const ready = () => v.paused && this._want?.kind === "loading" && this._message(null);
    v.addEventListener("canplay", ready);
    v.addEventListener("seeked", ready);
    v.addEventListener("seeked", () => {
      if (this._seekTarget != null && this._seekDeadline === Infinity) this._seekDeadline = Date.now() + 3000;
      this._paint();
    });
    v.addEventListener("ended", () => this._syncPlay());
    v.addEventListener("error", () => this._feedFailed(v.error));
    this._scrub.addEventListener("pointerdown", (e) => this._scrubStart(e));
    this._stage = $(".stage");
    this._card = $("ha-card");
    // Ticks are as dense as the bar's width allows.
    this._resizeObs = new ResizeObserver(() => requestAnimationFrame(() => (this._fit(), this._drawCoverage())));
    this._resizeObs.observe(this);
    requestAnimationFrame(() => this._fit());
  }

  /** Narrow the video (16:9) until the whole card - chips to scrub bar - fits on the screen. */
  _fit() {
    const st = this._stage;
    if (!st || !this._card || document.fullscreenElement) return;
    const width = this._card.clientWidth;
    if (!width) return;
    // Everything but the video, and where the card starts (below HA's toolbar).
    const chrome = this._card.offsetHeight - st.offsetHeight;
    const top = Math.min(Math.max(this.getBoundingClientRect().top + window.scrollY, 0), 200);
    const avail = Math.max(window.innerHeight - top - chrome - 8, 160);
    const w = Math.min(width, Math.floor((avail * 16) / 9));
    const px = w < width ? `${w}px` : "";
    if (st.style.width !== px) st.style.width = px;
  }

  /**
   * The veil over the video: kind "loading" (a spinner; shown only if it
   * lasts TL_LOADING_DELAY_MS), "empty" (nothing to show) or "error". No text: hidden.
   */
  _message(text, kind = "loading", sub = "") {
    if (!this._veil) return;
    clearTimeout(this._veilTimer);
    this._want = text ? { kind } : null;
    const show = () => {
      const v = this._veil;
      v.classList.toggle("off", !text);
      if (!text) return;
      v.classList.remove("loading", "empty", "error");
      v.classList.add(kind);
      v.querySelector("ha-icon").setAttribute("icon", kind === "error" ? "mdi:alert-circle-outline" : "mdi:video-off-outline");
      v.querySelector(".vtext").textContent = kind === "loading" ? `${text}…` : text;
      v.querySelector(".vsub").textContent = sub;
    };
    if (text && kind === "loading" && this._veil.classList.contains("off")) this._veilTimer = setTimeout(show, TL_LOADING_DELAY_MS);
    else show();
  }

  /** A stall (seek, buffering): says so, unless the veil already says it or says more (an error). */
  _stall(text) {
    if (!this._want) this._message(text);
  }

  _camera() {
    return this._index?.cameras.find((c) => c.id === this._cameraId);
  }

  _renderCameras() {
    const box = this.shadowRoot.querySelector(".cams");
    box.innerHTML = "";
    for (const c of this._index.cameras) {
      const b = document.createElement("button");
      b.textContent = c.name;
      b.dataset.id = c.id;
      b.addEventListener("click", () => this._selectCamera(c.id));
      box.append(b);
    }
  }

  _selectCamera(id) {
    this._cameraId = id;
    tlPrefs("camera", id);
    for (const b of this.shadowRoot.querySelectorAll(".cams button")) b.classList.toggle("on", Number(b.dataset.id) === id);
    const days = this._camera()?.days ?? [];
    const box = this.shadowRoot.querySelector(".days");
    box.innerHTML = "";
    for (const d of days) {
      const b = document.createElement("button");
      b.className = "day";
      b.dataset.date = d.date;
      const mid = (d.start + d.end) / 2;
      b.innerHTML = `<small>${this._fmt(mid, { weekday: "short" })}</small>${this._fmt(mid, { month: "numeric", day: "numeric" })}`;
      b.addEventListener("click", () => this._selectDay(d.date));
      box.append(b);
    }
    // Stay on the same date across cameras, if this one has it.
    const keep = days.find((d) => d.date === this._date);
    if (days.length) this._selectDay((keep ?? days[0]).date);
    else {
      this._closeFeed();
      this._message("No time-lapse for this camera yet", "empty");
    }
  }

  _stepDay(dir) {
    const days = this._camera()?.days ?? [];
    const i = days.findIndex((d) => d.date === this._date);
    const j = i + dir;
    if (i >= 0 && j >= 0 && j < days.length) this._selectDay(days[j].date);
  }

  _closeFeed() {
    this._feed?.close();
    const v = this._video;
    if (!this._feed && v?.getAttribute("src")) {
      // Native HLS: stop the old day (and its transcodes); load() drops its queued events.
      v.pause();
      v.removeAttribute("src");
      v.load();
    }
    this._feed = null;
    this._session = null;
    this._seekTarget = null;
    this._paint();
  }

  /** Open a day; at: the wall time to start from (default its start). */
  async _selectDay(date, at = null, playing = true, codec = null) {
    this._date = date;
    this._resume = null;
    this._played = false;
    const days = this._camera()?.days ?? [];
    const i = days.findIndex((d) => d.date === date);
    this.shadowRoot.querySelector(".prev").disabled = i < 0 || i >= days.length - 1;
    this.shadowRoot.querySelector(".next").disabled = i <= 0;
    for (const b of this.shadowRoot.querySelectorAll(".days button")) {
      const on = b.dataset.date === date;
      b.classList.toggle("on", on);
      if (on) b.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    this._day = days[i];
    this._closeFeed();
    this._followPausedUntil = 0;
    this._setView(at ?? this._day?.start);
    if (at !== null) this._paint(at); // there already, while the day opens
    this._message("Loading", "loading", this._day ? this._fmt((this._day.start + this._day.end) / 2, { weekday: "short", month: "short", day: "numeric" }) : "");
    const seq = ++this._seq;
    codec ??= TL_MSE?.isTypeSupported(HEVC_PROBE) || (!TL_MSE && this._video.canPlayType(HEVC_PROBE)) ? "hevc" : "h264";
    let session;
    try {
      session = await this._ws({ type: "surveillance_station/timelapse", camera_id: this._cameraId, date, codec });
    } catch (e) {
      if (seq === this._seq) {
        // Play tries again from where it was asked to start.
        this._resume = { at };
        this._syncPlay();
        this._message("Couldn't open the time-lapse", "error", e.message || e.code || String(e));
      }
      return;
    }
    if (seq !== this._seq || !this.isConnected) return;
    if (!session.url) {
      this._message("Nothing recorded on this day", "empty");
      return;
    }
    this._session = session;
    this._titleSkips();
    const v = this._video;
    if (TL_MSE) {
      this._feed = new SegmentFeed(v, session, {
        onError: (e) => this._feedFailed(e),
        onWaiting: () => this._stall("Loading"),
      });
    } else {
      v.src = session.url;
    }
    const start = at === null ? 0 : this._mediaAt(at);
    if (start > 0) this._seek(start);
    else this._paint();
    if (playing) this._play();
    else this._message(null);
  }

  _feedFailed(e) {
    if (!this._session) return;
    // One time-lapse plays at a time: a newer one (another card, device or
    // viewer) ended this. Play opens the day again, where it was.
    const wall = this._wallAt(this._seekTarget ?? this._video.currentTime);
    if (e?.status === 410) {
      this._resume = { at: wall };
      this._closeFeed();
      this._syncPlay();
      this._message("Playing elsewhere", "empty", "One time-lapse plays at a time. Press play to take it back.");
      return;
    }
    // The session is gone (HA restarted, or it expired): open the day again, where it was.
    if (e?.status === 404 && !this._reopened) {
      this._reopened = true;
      setTimeout(() => (this._reopened = false), 30_000);
      this._selectDay(this._date, wall, !this._video.paused, this._session.codec);
      return;
    }
    // H.265 the browser said it plays, but not this stream's profile/level: H.264 then.
    if (e?.codec?.startsWith("hvc1") && this._session.codec === "hevc") {
      this._selectDay(this._date, wall, true, "h264");
      return;
    }
    // Stop here (no refetching the failing segment on every timeupdate); play tries again from here.
    this._resume = { at: wall };
    this._closeFeed();
    this._syncPlay();
    this._message("Playback failed", "error", e?.message || e?.code || String(e));
  }

  _play() {
    this._video.play()?.catch(() => this._syncPlay());
  }

  _syncPlay() {
    const paused = this._video.paused;
    this._playBtn.querySelector("ha-icon").setAttribute("icon", paused ? "mdi:play" : "mdi:pause");
    this._playBtn.title = paused ? "Play" : "Pause";
  }

  /** Wall time at media time m (the run holding it; past a run's end, its end). */
  _wallAt(m) {
    const runs = this._session?.runs ?? [];
    let run = runs[0];
    for (const r of runs) if (r.media_start <= m) run = r;
    if (!run) return null;
    return run.wall_start + Math.min(Math.max(0, m - run.media_start), run.duration) * run.rate;
  }

  /** Media time showing wall time w (the next run's start if w falls in a gap). */
  _mediaAt(w) {
    const runs = this._session?.runs ?? [];
    for (const r of runs) {
      if (w < r.wall_start) return r.media_start;
      if (w < r.wall_start + r.duration * r.rate) return r.media_start + (w - r.wall_start) / r.rate;
    }
    const last = runs.at(-1);
    return last ? last.media_start + last.duration - 0.1 : 0;
  }

  /** Where wall time w is on the scrub bar (0..1 of the view; outside it, beyond). */
  _frac(w) {
    const v = this._view;
    return v ? (w - v.start) / (v.end - v.start) : 0;
  }

  /** Show span seconds of the day around wall time `center` (whole day at most, never past its ends). */
  _setView(center, paint = true) {
    const d = this._day;
    if (!d) {
      this._view = null;
    } else {
      // The widest level is the whole day, a 23 or 25 h one too.
      const span = this._span >= TL_SPANS[0][0] ? d.end - d.start : Math.min(this._span, d.end - d.start);
      const start = Math.max(d.start, Math.min(center - span / 2, d.end - span));
      this._view = { start, end: start + span };
    }
    this._drawCoverage();
    if (paint) this._paint();
  }

  _zoom(span) {
    this._span = span;
    tlPrefs("span", span);
    this._markSpan();
    this._followPausedUntil = 0;
    const w = this._session ? this._wallAt(this._seekTarget ?? this._video.currentTime) : null;
    this._setView(w ?? (this._view ? (this._view.start + this._view.end) / 2 : this._day?.start));
  }

  _pan(dir) {
    if (!this._view) return;
    const span = this._view.end - this._view.start;
    this._followPausedUntil = Date.now() + TL_FOLLOW_PAUSE_MS;
    this._setView(this._view.start + span / 2 + (dir * span) / 2);
  }

  _markSpan() {
    for (const b of this.shadowRoot.querySelectorAll("[data-span]")) {
      const on = Number(b.dataset.span) === this._span;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    }
  }

  /** Seek to media time t; the playhead is there at once, however long the load takes. */
  _seek(t) {
    this._seekFrom = this._seekTarget == null ? this._video.currentTime : this._seekFrom;
    this._seekTarget = t;
    this._seekDeadline = Infinity;
    this._video.currentTime = t;
    this._paint();
  }

  /** Skip d seconds of the time-lapse (d times its rate of real time). */
  _skip(d) {
    const v = this._video;
    if (!this._session) return;
    const end = this._session.duration ?? v.duration;
    // From where the last skip went, if it hasn't landed yet: skips add up.
    const t = Math.min(Math.max(0, (this._seekTarget ?? v.currentTime) + d), Number.isFinite(end) ? end - 0.1 : Infinity);
    this._followPausedUntil = 0;
    this._seek(t);
  }

  /** Skip buttons say how much real time they skip (at the day's first rate). */
  _titleSkips() {
    const rate = this._session?.runs?.[0]?.rate ?? 0;
    for (const b of this.shadowRoot.querySelectorAll("[data-skip]")) {
      const d = Number(b.dataset.skip);
      const real = Math.abs(d) * rate;
      const approx = real >= 3600 ? `${+(real / 3600).toFixed(1)} h` : `${Math.round(real / 60)} min`;
      b.title = `${d < 0 ? "Back" : "Forward"} ${Math.abs(d)} s${rate ? ` (≈${approx} of real time)` : ""}`;
    }
  }

  _drawCoverage() {
    const track = this.shadowRoot.querySelector(".track");
    const ticks = this.shadowRoot.querySelector(".ticks");
    const range = this.shadowRoot.querySelector(".range");
    track.innerHTML = "";
    ticks.innerHTML = "";
    const d = this._day;
    const view = this._view;
    for (const b of this.shadowRoot.querySelectorAll("[data-pan]")) {
      b.disabled = !view || (Number(b.dataset.pan) < 0 ? view.start <= d.start : view.end >= d.end);
    }
    if (range) range.textContent = "";
    if (!d || !view) return;
    const hm = (t) => (t >= d.end ? "24:00" : this._fmt(t, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }));
    const whole = view.end - view.start >= d.end - d.start;
    if (range && !whole) range.textContent = `${hm(view.start)} – ${hm(view.end)}`;
    for (const [a, b] of d.covered) {
      if (b <= view.start || a >= view.end) continue;
      const el = document.createElement("div");
      el.className = "cov";
      const l = Math.max(this._frac(a), 0);
      el.style.left = `${l * 100}%`;
      el.style.width = `${(Math.min(this._frac(b), 1) - l) * 100}%`;
      track.append(el);
    }
    // Ticks as dense as their labels allow (~64 px each), on the NAS clock's
    // round times: hours are counted from midnight on its clock, so a DST
    // day (23 or 25 h) still has them at 06:00, 12:00...
    const span = view.end - view.start;
    const px = Math.max(this._scrub?.clientWidth ?? 0, 1);
    const step = TL_TICK_STEPS.find((v) => (v * px) / span >= 64) ?? TL_TICK_STEPS.at(-1);
    const at = [];
    if (step < 3600) {
      for (let t = d.start + Math.ceil((view.start - d.start) / step) * step; t <= view.end + 1; t += step) at.push(t);
    } else {
      const hours = step / 3600;
      for (let t = d.start; t <= Math.min(view.end, d.end) + 1; t += 3600) {
        const h = Number(this._fmt(t, { hour: "numeric", hourCycle: "h23" }));
        if (t >= view.start - 1 && (h % hours === 0 || t >= d.end)) at.push(t);
      }
    }
    for (const t of at) {
      const f = this._frac(Math.min(t, d.end));
      const s = document.createElement("span");
      s.style.left = `${f * 100}%`;
      if (f < 0.04) s.className = "l";
      else if (f > 0.96) s.className = "r";
      s.textContent = hm(Math.min(t, d.end));
      ticks.append(s);
    }
  }

  _paint(wall = null) {
    if (!this._headEl) return;
    // While the head is being dragged, it is where the finger is.
    if (wall === null && this._dragging) return;
    // A seek under way shows where it goes until the video says it is there
    // (some browsers report the old time until the segment there has loaded,
    // even past "seeked"); the feed may move it on by a hair over a hole.
    const vid = this._video;
    if (this._seekTarget != null && !vid.seeking) {
      const ct = vid.currentTime;
      // There (not still where it came from), or it settled elsewhere for good
      // (a few seconds after landing: the end, a feed moving on).
      const there = ct !== this._seekFrom && ct >= this._seekTarget - 0.25 && ct - this._seekTarget < 2;
      if (there || Date.now() > this._seekDeadline) this._seekTarget = null;
    }
    const w = wall ?? (this._session ? this._wallAt(this._seekTarget ?? vid.currentTime) : null);
    this._headEl.style.display = w === null ? "none" : "";
    this._clock.style.display = w === null ? "none" : "";
    if (w === null) return;
    // Zoomed in, the view follows the playhead (not for a while after a pan).
    const v = this._view;
    const d = this._day;
    if (v && d && !this._dragging && (w < v.start || w > v.end) && w >= d.start && w <= d.end && Date.now() >= this._followPausedUntil) {
      this._setView(w + (v.end - v.start) * 0.3, false);
    }
    const f = this._frac(w);
    this._headEl.style.display = f < 0 || f > 1 ? "none" : "";
    this._headEl.style.left = `${f * 100}%`;
    this._clock.textContent = this._fmt(w, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  _scrubStart(e) {
    if (!this._session || !this._day || !this._view) return;
    const rect = this._scrub.getBoundingClientRect();
    const { start, end } = this._view;
    const wallAt = (x) => start + Math.min(1, Math.max(0, (x - rect.left) / rect.width)) * (end - start);
    this._scrub.setPointerCapture(e.pointerId);
    this._dragging = true;
    let w = wallAt(e.clientX);
    this._paint(w);
    const scrub = this._scrub;
    const move = (ev) => ev.pointerId === e.pointerId && this._paint((w = wallAt(ev.clientX)));
    // Capture ends however the drag does (up, cancel, the bar re-rendered).
    const up = (ev) => {
      if (ev.pointerId !== e.pointerId) return;
      scrub.removeEventListener("pointermove", move);
      scrub.removeEventListener("lostpointercapture", up);
      this._dragging = false;
      if (scrub === this._scrub && this._session) this._seek(this._mediaAt(w));
    };
    scrub.addEventListener("pointermove", move);
    scrub.addEventListener("lostpointercapture", up);
  }
}

if (!customElements.get(TL_TAG)) {
  customElements.define(TL_TAG, SSTimelapseCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: TL_TAG,
    name: "Surveillance Station time-lapse",
    description: "Watch a camera's Surveillance Station time-lapse, one day at a time.",
  });
}
