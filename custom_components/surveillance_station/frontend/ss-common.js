/*
 * What the two cards share: MSE and the MP4 init-segment parsers, the SS
 * stream message parser, the timeline's ticks and live view, the veil over the
 * video, per-viewer preferences, and how controls are labelled.
 *
 * Each card loads this with its own version query (see the import at the top
 * of each), as the timeline card loads the time-lapse card, so a release never
 * runs a new card against a stale cached copy of this module. No DOM at load
 * time: tests/js runs the pure parts under node.
 */

// MSE for SS's stream and the time-lapse segments: iOS Safari (17.1+) only has
// ManagedMediaSource. Without either, the timeline card's live falls back to
// the newest recordings (HLS, ~20 s behind), and the time-lapse card hands the
// <video> its playlist.
export const MSE = globalThis.MediaSource ?? globalThis.ManagedMediaSource;

// A transparent poster: without one, Android WebView (the HA app) paints its
// default poster, a big grey play arrow, over a video with no frame yet, so
// it flashed on every jump to a bookmark or new window.
export const BLANK_POSTER = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

export const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/**
 * Per-viewer preferences, under a key prefix of the card's. Storage can be
 * unavailable (private mode, WebView settings); the card then just uses its
 * config.
 */
export function prefsFor(prefix) {
  return {
    get(key, fallback) {
      try {
        const v = localStorage.getItem(`${prefix}${key}`);
        return v == null ? fallback : JSON.parse(v);
      } catch (e) {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(`${prefix}${key}`, JSON.stringify(value));
      } catch (e) {
        /* not persisted */
      }
    },
  };
}

// ---- controls ---------------------------------------------------------------

/** A control's name, as its tooltip and for screen readers (an icon alone has none). */
export const labelAttrs = (text) => `title="${esc(text)}" aria-label="${esc(text)}"`;

export function setLabel(el, text) {
  el.title = text;
  el.setAttribute("aria-label", text);
}

// The date formats, one per time zone, language and options: a format keeps
// the zone it was made in, and a device can move to another while the page
// stays open. Every Intl.DateTimeFormat of hour12Of and muteEnds comes from
// here, so a language tag Intl doesn't know gets the browser's in both,
// instead of throwing halfway through a card update.
const dateFormats = new Map();
function dateFormat(language, opts) {
  const key = `${new Intl.DateTimeFormat().resolvedOptions().timeZone}|${language}|${JSON.stringify(opts)}`;
  let f = dateFormats.get(key);
  if (!f) {
    try {
      f = new Intl.DateTimeFormat(language, opts);
    } catch (e) {
      try {
        f = new Intl.DateTimeFormat(undefined, opts); // the browser's language
      } catch (e2) {
        f = new Intl.DateTimeFormat(undefined, { ...opts, timeZone: undefined }); // and its zone, for one Intl doesn't know
      }
    }
    dateFormats.set(key, f);
  }
  return f;
}

/**
 * Whether times read 12 h, as the user's HA profile says (hass.locale.time_format:
 * "12", "24", "language" or "system").
 */
export function hour12Of(locale) {
  const f = locale?.time_format;
  if (f === "12" || f === "24") return f === "12";
  const lang = f === "system" ? undefined : locale?.language;
  return Boolean(dateFormat(lang, { hour: "numeric" }).resolvedOptions().hour12);
}

/**
 * A slider's value after a key: the arrows move it by `step`, Page Up / Down
 * by `page`, Home / End to its ends, always within [min, max]. null: a key
 * the slider leaves alone.
 */
export function sliderKey(key, value, { min, max, step, page = step * 10 }) {
  const to = {
    ArrowLeft: value - step,
    ArrowDown: value - step,
    ArrowRight: value + step,
    ArrowUp: value + step,
    PageDown: value - page,
    PageUp: value + page,
    Home: min,
    End: max,
  }[key];
  return to === undefined ? null : Math.min(Math.max(to, min), max);
}

/**
 * A slider's value, within [min, max], and what it says. The text alone
 * doesn't tell whether the value is current: after a pan or zoom while paused
 * it is the same, but the old value can be outside the new range.
 */
export function setSliderValue(el, value, text, min, max) {
  const now = String(Math.round(Math.min(Math.max(value, min), max)));
  if (el.getAttribute("aria-valuenow") === now && el.getAttribute("aria-valuetext") === text) return;
  el.setAttribute("aria-valuenow", now);
  el.setAttribute("aria-valuetext", text);
}

/**
 * Whether a bookmark (by its name, "Person, Car") or a result (its kind) is
 * of one of the kinds chosen; none chosen: all are. Built once per list drawn,
 * not per bookmark.
 */
export function kindTest(kinds) {
  if (!kinds.size) return () => true;
  const want = new Set([...kinds].map((k) => k.toLowerCase()));
  return (name) => String(name ?? "").split(",").some((k) => want.has(k.trim().toLowerCase()));
}

// ---- the veil over a video -------------------------------------------------

/** Spinner (loading) or icon, a line, a sub-line; Retry where the card can retry. */
export const veilHtml = (retry = false) => `
  <div class="veil off">
    <div class="spin"></div>
    <ha-icon></ha-icon>
    <div class="vtext"></div>
    <div class="vsub"></div>${retry ? `\n    <button data-act="retry">Retry</button>` : ""}
  </div>`;

export const VEIL_CSS = `
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
`;

/**
 * Show a veil: kind "loading" (spinner), "empty" (nothing to show here) or
 * "error" (with Retry, if it has one). Empty text hides it.
 */
export function setVeil(veil, text, kind = "loading", sub = "") {
  veil.classList.toggle("off", !text);
  if (!text) return;
  veil.classList.remove("loading", "empty", "error");
  veil.classList.add(kind);
  veil.querySelector("ha-icon").setAttribute("icon", kind === "error" ? "mdi:alert-circle-outline" : "mdi:video-off-outline");
  veil.querySelector(".vtext").textContent = kind === "loading" ? `${text}…` : text;
  veil.querySelector(".vsub").textContent = sub;
}

// ---- the timeline ----------------------------------------------------------

/**
 * Tick times in [start, end], every `step` seconds on the local clock: hour
 * and day steps are counted in local time, so they stay on whole hours and
 * midnights across a DST change (a day is then 23 or 25 hours).
 */
export function ticksOf(start, end, step) {
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

/**
 * Where a view `span` seconds wide and `px` pixels across ends while it shows
 * now: 5 % of the span past it, on the view's pixel step. Until now has moved
 * a pixel the view is exactly the same (a reload finds nothing to redraw),
 * and now stays on the same pixel of it.
 */
export function liveViewEnd(now, span, px) {
  const step = span / px;
  return (Math.round(now / step) + Math.round(px * 0.05)) * step;
}

// ---- SS's stream and MP4 -----------------------------------------------------

const headerText = new TextDecoder();

/**
 * Parse one SS stream message: 4-byte header end, query-string header,
 * payload. null for a message too short, or whose header end points outside it.
 */
export function readStreamMsg(buf) {
  const b = new Uint8Array(buf);
  if (b.length <= 4) return null;
  const end = ((b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]) >>> 0;
  // Out of range, the payload would be read as header or the other way round.
  if (end < 4 || end > b.length) return null;
  const head = {};
  for (const pair of headerText.decode(b.subarray(4, end)).split("&")) {
    const i = pair.indexOf("=");
    if (i > 0) head[pair.slice(0, i)] = pair.slice(i + 1);
  }
  return { head, data: b.subarray(end) };
}

export function findBox(buf, tag) {
  const t = [...tag].map((c) => c.charCodeAt(0));
  for (let i = 4; i + 4 <= buf.length; i++)
    if (buf[i] === t[0] && buf[i + 1] === t[1] && buf[i + 2] === t[2] && buf[i + 3] === t[3]) return i;
  return -1;
}

/** RFC 6381 codec string from an init segment (moov with hvcC / avcC), or null. */
export function codecOf(moov) {
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
export function audioCodecOf(moov) {
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

// ---- The mute card ---------------------------------------------------------------

const MUTE_TEXT = {
  en: {
    title: "Notifications", everything: "Everything", byKind: "By kind", cameras: "Cameras", forever: "forever",
    until: "until {t}", muteFor: "Mute {n} h", unmuteAll: "Unmute all", none: "No Surveillance Station mute switches found.",
    muted: "muted", otherKinds: "other kinds", kinds: { person: "Person", car: "Car", animal: "Animal" }, kindsOf: "Kinds on {camera}",
    muteAll: "Mute everything", muteKind: "Mute {kind} on every camera", muteCamera: "Mute {camera}", muteCameraKind: "Mute {kind} on {camera}",
  },
  zh: {
    title: "通知", everything: "全部", byKind: "按类型", cameras: "摄像头", forever: "永久",
    until: "至 {t}", muteFor: "静音 {n} 小时", unmuteAll: "全部取消静音", none: "未找到 Surveillance Station 的静音开关。",
    muted: "已静音", otherKinds: "其他类型", kinds: { person: "人", car: "车", animal: "动物" }, kindsOf: "{camera}的类型",
    muteAll: "全部静音", muteKind: "静音所有摄像头的{kind}", muteCamera: "静音{camera}", muteCameraKind: "静音{camera}的{kind}",
  },
};

/** The mute card's texts for a UI language (English for the ones it has none of). */
export const muteText = (language) => MUTE_TEXT[String(language ?? "").toLowerCase().startsWith("zh") ? "zh" : "en"];

/**
 * A kind's name in the card's texts; one it has no name for (a Frigate label's) capitalized.
 * Own names only: a label such as "constructor" must not find the object's inherited function.
 */
export const kindLabel = (text, kind) => (Object.hasOwn(text.kinds, kind) ? text.kinds[kind] : kind[0].toUpperCase() + kind.slice(1));

/** A text with its {name} places filled in (a function, so a "$&" in a camera's name stays as it is). */
export const fillText = (template, values) => template.replace(/\{(\w+)\}/g, (_, k) => values[k]);

/**
 * What is still muted on a camera whose *all kinds* switch is off (`camera`:
 * muteGroups' `all` and `kinds`): its kinds whose switches are on, and "other
 * kinds" when the switch says others_muted (kinds without a switch there, left
 * muted when its kinds were turned off one by one). "" when nothing is.
 */
export function partlyMuted(text, states, camera) {
  const names = Object.entries(camera.kinds).filter(([, id]) => states?.[id]?.state === "on").map(([k]) => kindLabel(text, k));
  if (states?.[camera.all]?.attributes?.others_muted === true) names.push(text.otherKinds);
  return names.length ? `${names.join(", ")} · ${text.muted}` : "";
}

/**
 * How long a mute switch's mute lasts, as text in `timeZone` (default: the
 * browser's; see serverZone) and the user's 12 / 24 h setting: "forever" (on
 * without muted_until: until lifted), "until 20:00" today, with the day (and
 * year) otherwise, "" when off.
 */
export function muteEnds(state, text, locale, now = Date.now(), timeZone = undefined) {
  if (state?.state !== "on") return "";
  const until = state.attributes?.muted_until;
  if (until == null) return text.forever;
  const t = new Date(until);
  if (Number.isNaN(t.getTime())) return "";
  // The days as year-month-day in the zone: the same day is the same text, and the year its first four.
  const day = (d) => dateFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone }).format(d);
  // h23, not hour12: false, which some browsers show as 24:00 at midnight.
  const opts = { hour: "numeric", minute: "2-digit", hourCycle: hour12Of(locale) ? "h12" : "h23", timeZone };
  const [ends, today] = [day(t), day(new Date(now))];
  if (ends !== today) {
    Object.assign(opts, { month: "short", day: "numeric" }, ends.slice(0, 4) !== today.slice(0, 4) ? { year: "numeric" } : {});
  }
  return fillText(text.until, { t: dateFormat(locale?.language || undefined, opts).format(t) });
}

/**
 * The time zone HA's profile asks times in: the server's when it says "server"
 * (hass.locale.time_zone), else undefined: the browser's.
 */
export const serverZone = (hass) => (hass?.locale?.time_zone === "server" ? hass.config?.time_zone : undefined);

/** The hours the card's buttons mute everything for: its `durations` that are positive numbers (default 1 and 8). */
export function muteDurations(durations) {
  if (!Array.isArray(durations)) return [1, 8];
  return durations.map(Number).filter((n) => Number.isFinite(n) && n > 0);
}

/**
 * The entities that may be mute switches: this integration's switches in
 * hass.entities (the registry, which changes far less often than the states).
 */
export const muteSwitchIds = (hass) =>
  Object.entries(hass?.entities ?? {})
    .filter(([id, e]) => id.startsWith("switch.") && e?.platform === "surveillance_station")
    .map(([id]) => id);

/**
 * The mute switches among `ids`, laid out for the card: one group per device
 * (a Surveillance Station entry) with `all` (everything), `kinds` (kind ->
 * entity id, for every camera) and `cameras` (by name, each with `all` and
 * `kinds`). A mute switch has the `camera` and `locked` attributes; `camera`
 * and `kind` say which camera and kind it is for.
 */
export function muteGroups(hass, ids = muteSwitchIds(hass)) {
  const groups = new Map();
  for (const id of ids) {
    const attrs = hass.states?.[id]?.attributes;
    if (!attrs || !("camera" in attrs) || !("locked" in attrs)) continue;
    const device = hass.entities?.[id]?.device_id ?? "";
    if (!groups.has(device)) groups.set(device, { device, all: null, kinds: {}, cameras: [] });
    const group = groups.get(device);
    if (attrs.camera == null) {
      if (attrs.kind == null) group.all = id;
      else group.kinds[attrs.kind] = id;
      continue;
    }
    let camera = group.cameras.find((c) => c.name === attrs.camera);
    if (!camera) group.cameras.push((camera = { name: attrs.camera, all: null, kinds: {} }));
    if (attrs.kind == null) camera.all = id;
    else camera.kinds[attrs.kind] = id;
  }
  const out = [...groups.values()];
  for (const g of out) g.cameras.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}
