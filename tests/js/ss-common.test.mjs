// The cards' pure helpers (ss-common.js), on small init segments and stream
// messages built the way SS and ffmpeg write them.
//   node --test "tests/js/*.test.mjs"
import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";
import {
  audioCodecOf, codecOf, esc, findBox, kindTest, labelAttrs, liveViewEnd, prefsFor, readStreamMsg, setSliderValue, setVeil,
  sliderKey, ticksOf, veilHtml,
} from "../../custom_components/surveillance_station/frontend/ss-common.js";

// ---- MP4 boxes -----------------------------------------------------------------

const bytes = (s) => [...s].map((c) => c.charCodeAt(0));
const u16 = (n) => [(n >>> 8) & 255, n & 255];
const u32 = (n) => [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
const box = (type, ...parts) => {
  const body = parts.flat();
  return [...u32(8 + body.length), ...bytes(type), ...body];
};
const fullBox = (type, ...parts) => box(type, [0, 0, 0, 0], ...parts); // version 0, no flags
const moov = (stsdEntry, handler) =>
  new Uint8Array(
    box("moov", fullBox("mvhd", Array(96).fill(0)),
      box("trak", box("mdia", fullBox("hdlr", u32(0), bytes(handler), Array(12).fill(0), [0]),
        box("minf", box("stbl", fullBox("stsd", u32(1), stsdEntry)))))),
  );

// VisualSampleEntry: 78 bytes (1920x1080, 72 dpi, one frame per sample, 24-bit), then its config box.
const visual = [...Array(6).fill(0), ...u16(1), ...Array(16).fill(0), ...u16(1920), ...u16(1080),
  ...u32(0x480000), ...u32(0x480000), ...u32(0), ...u16(1), ...Array(32).fill(0), ...u16(0x18), 0xff, 0xff];
// HEVCDecoderConfigurationRecord up to numOfArrays (none).
const hvcC = ({ profile, compat, constraints, level }) =>
  box("hvcC", [1, profile, ...u32(compat), ...constraints, level, 0xf0, 0x00, 0xfc, 0xfd, 0xf8, 0xf8, 0, 0, 0x0f, 0]);
const video = (entry, config) => moov(box(entry, visual, config), "vide");

// AudioSampleEntry (stereo, 16 bit, 16 kHz), then an esds whose descriptor
// lengths take 4 bytes each, as ffmpeg writes them.
const audioEntry = [...Array(6).fill(0), ...u16(1), ...Array(8).fill(0), ...u16(2), ...u16(16), ...u32(0), ...u32(16000 << 16)];
const len = (n) => [0x80, 0x80, 0x80, n];
const esds = (objectType, audioSpecificConfig) => {
  const dsi = [0x05, ...len(audioSpecificConfig.length), ...audioSpecificConfig];
  const dcd = [0x04, ...len(13 + dsi.length), objectType, 0x15, 0, 0, 0, ...u32(128000), ...u32(96000), ...dsi];
  const sl = [0x06, ...len(1), 0x02];
  return fullBox("esds", [0x03, ...len(3 + dcd.length + sl.length), ...u16(1), 0, ...dcd, ...sl]);
};
const audio = (entry, ...config) => moov(box(entry, audioEntry, ...config), "soun");

describe("codecOf", () => {
  const main = { profile: 0x01, compat: 0x60000000, constraints: [0xb0, 0, 0, 0, 0, 0], level: 153 };

  test("H.265 Main as SS's stream has it (hev1), level 5.1", () => {
    assert.equal(codecOf(video("hev1", hvcC(main))), "hev1.1.6.L153.B0");
  });

  test("H.265 tagged hvc1 (HA's time-lapse transcode) says hvc1", () => {
    assert.equal(codecOf(video("hvc1", hvcC(main))), "hvc1.1.6.L153.B0");
  });

  test("the compatibility flags are written bit-reversed", () => {
    // Flag j is bit 31 - j: Main 10 alone (flag 2, 0x20000000) reads 4, Main alone (flag 1) 2.
    const main10 = { ...main, profile: 0x02, compat: 0x20000000, level: 150 };
    assert.equal(codecOf(video("hvc1", hvcC(main10))), "hvc1.2.4.L150.B0");
    assert.equal(codecOf(video("hvc1", hvcC({ ...main, compat: 0x40000000 }))), "hvc1.1.2.L153.B0");
    assert.equal(codecOf(video("hvc1", hvcC({ ...main, compat: 0x00000001 }))), "hvc1.1.80000000.L153.B0");
  });

  test("tier, profile space and constraint bytes", () => {
    const high = { ...main, profile: 0x20 | 0x02, compat: 0x20000000 };
    assert.equal(codecOf(video("hvc1", hvcC(high))), "hvc1.2.4.H153.B0");
    assert.equal(codecOf(video("hvc1", hvcC({ ...main, profile: 0x40 | 0x01 }))), "hvc1.A1.6.L153.B0");
    // Trailing zero bytes are left out, the ones between kept.
    assert.equal(codecOf(video("hvc1", hvcC({ ...main, constraints: [0x90, 0, 0x08, 0, 0, 0] }))), "hvc1.1.6.L153.90.0.8");
  });

  test("H.264 from its avcC", () => {
    const avcC = box("avcC", [1, 0x64, 0x00, 0x28, 0xff, 0xe1, ...u16(4), 0x67, 0x64, 0x00, 0x28, 1, ...u16(4), 0x68, 0xee, 0x3c, 0x80]);
    assert.equal(codecOf(video("avc1", avcC)), "avc1.640028");
  });

  test("neither: null", () => {
    assert.equal(codecOf(audio("mp4a", esds(0x40, [0x12, 0x10]))), null);
  });

  test("findBox finds a box's type, past the first 4 bytes", () => {
    const m = video("hev1", hvcC(main));
    assert.deepEqual([...m.subarray(findBox(m, "hvcC"), findBox(m, "hvcC") + 4)], bytes("hvcC"));
    assert.equal(findBox(m, "avcC"), -1);
  });
});

describe("audioCodecOf", () => {
  test("AAC-LC, HE-AAC and an escaped object type from the esds", () => {
    assert.equal(audioCodecOf(audio("mp4a", esds(0x40, [0x12, 0x10]))), "mp4a.40.2");
    assert.equal(audioCodecOf(audio("mp4a", esds(0x40, [0x2b, 0x92, 0x08, 0x00]))), "mp4a.40.5");
    // 31 in the first 5 bits: the type is 32 + the next 6 (USAC, 42).
    assert.equal(audioCodecOf(audio("mp4a", esds(0x40, [0xf9, 0x40, 0x00]))), "mp4a.40.42");
  });

  test("another object type than MPEG-4 audio (MP3 in mp4a)", () => {
    assert.equal(audioCodecOf(audio("mp4a", esds(0x6b, []))), "mp4a.6b");
  });

  test("other sample entries by name", () => {
    assert.equal(audioCodecOf(audio("Opus", box("dOps", [0, 2, 0, 0, ...u32(48000), 0, 0, 0]))), "opus");
    assert.equal(audioCodecOf(audio("ulaw")), "ulaw");
    assert.equal(audioCodecOf(audio("samr")), "samr");
  });

  test("nothing to go on: AAC-LC", () => {
    assert.equal(audioCodecOf(new Uint8Array(box("moov", box("trak", [])))), "mp4a.40.2");
    assert.equal(audioCodecOf(audio("mp4a")), "mp4a.40.2");
  });
});

// ---- SS's stream ------------------------------------------------------------------

const message = (header, payload = [], end = 4 + header.length) =>
  new Uint8Array([...u32(end), ...bytes(header), ...payload]).buffer;

describe("readStreamMsg", () => {
  test("a frame: its header and the fragment, a view of the message (not a copy)", () => {
    const moof = box("moof", box("mfhd", u32(0), u32(1)));
    const buf = message("mediaType=1&key=1&msec=1790293258123", moof);
    const { head, data } = readStreamMsg(buf);
    assert.deepEqual(head, { mediaType: "1", key: "1", msec: "1790293258123" });
    assert.deepEqual([...data], moof);
    assert.equal(data.buffer, buf);
  });

  test("the first message: a header alone", () => {
    const { head, data } = readStreamMsg(message("vdoCodec=H265&adoCodec=MPEG4-GENERIC"));
    assert.deepEqual(head, { vdoCodec: "H265", adoCodec: "MPEG4-GENERIC" });
    assert.equal(data.length, 0);
  });

  test("a value may hold '='; a pair without one is skipped", () => {
    assert.deepEqual(readStreamMsg(message("a=b=c&close&=x")).head, { a: "b=c" });
  });

  test("too short, or a header end outside the message: null", () => {
    assert.equal(readStreamMsg(new Uint8Array([0, 0, 0, 4]).buffer), null);
    assert.equal(readStreamMsg(message("key=1", [1, 2, 3], 2)), null);
    assert.equal(readStreamMsg(message("key=1", [1, 2, 3], 0xffffffff)), null);
  });

  test("a long header parses (no argument spread)", () => {
    const long = `x=${"a".repeat(300_000)}`;
    assert.equal(readStreamMsg(message(long)).head.x.length, 300_000);
  });
});

// ---- the timeline ------------------------------------------------------------------

describe("ticksOf", () => {
  const tz = process.env.TZ;
  afterEach(() => {
    if (tz === undefined) delete process.env.TZ;
    else process.env.TZ = tz;
  });
  const local = (t) => {
    const d = new Date(t * 1000);
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  };

  test("days on local midnights across the fall-back day (25 h)", () => {
    process.env.TZ = "America/Los_Angeles";
    const start = Date.UTC(2026, 9, 30, 12) / 1000; // Oct 30, 05:00 PDT
    const ticks = ticksOf(start, start + 4 * 86400, 86400);
    assert.deepEqual(ticks.map(local), ["10/31 0:00", "11/1 0:00", "11/2 0:00", "11/3 0:00"]);
    assert.deepEqual(ticks.slice(1).map((t, i) => t - ticks[i]), [86400, 90000, 86400]);
  });

  test("days across the spring-forward day (23 h)", () => {
    process.env.TZ = "America/Los_Angeles";
    const start = Date.UTC(2026, 2, 7, 9) / 1000; // Mar 7, 01:00 PST
    const ticks = ticksOf(start, start + 2 * 86400, 86400);
    assert.deepEqual(ticks.map(local), ["3/8 0:00", "3/9 0:00"]);
    assert.equal(ticks[1] - ticks[0], 82800);
  });

  test("hour steps stay on whole local hours through the repeated hour", () => {
    process.env.TZ = "America/Los_Angeles";
    const start = Date.UTC(2026, 10, 1, 6) / 1000; // Oct 31, 23:00 PDT
    const ticks = ticksOf(start, start + 12 * 3600, 3 * 3600);
    assert.deepEqual(ticks.map(local), ["11/1 0:00", "11/1 3:00", "11/1 6:00", "11/1 9:00"]);
    // 00:00 PDT to 03:00 PST is 4 hours.
    assert.equal(ticks[1] - ticks[0], 4 * 3600);
  });

  test("sub-hour steps on the local clock's quarter and half hours", () => {
    process.env.TZ = "Asia/Kolkata"; // UTC+5:30
    const start = Date.UTC(2026, 0, 1, 0, 10) / 1000; // 05:40 local
    assert.deepEqual(ticksOf(start, start + 3600, 1800).map(local), ["1/1 6:00", "1/1 6:30"]);
    assert.deepEqual(ticksOf(start, start + 1800, 900).map(local), ["1/1 5:45", "1/1 6:00"]);
  });

  test("the ends are included", () => {
    process.env.TZ = "UTC";
    assert.deepEqual(ticksOf(3600, 7200, 1800), [3600, 5400, 7200]);
    assert.deepEqual(ticksOf(3600, 7200, 3600), [3600, 7200]);
  });
});

describe("liveViewEnd", () => {
  // An hour over 997 px: a pixel is 3.61 s.
  const span = 3600;
  const px = 997;
  const step = span / px;
  const now = 1_790_000_000.3;
  const times = Array.from({ length: 400 }, (_, i) => now + i * 0.1); // 40 s, about 11 pixels
  const nowPixel = (t, end) => Math.round(((t - (end - span)) / span) * px);

  test("5 % of the span past now, within a pixel", () => {
    for (const t of times) assert.ok(Math.abs(liveViewEnd(t, span, px) - (t + span * 0.05)) <= step);
  });

  test("the same view until now has moved a pixel, then one pixel on", () => {
    // A Set: repeated ends must be the very same number, as the timeline's memo compares them.
    const ends = [...new Set(times.map((t) => liveViewEnd(t, span, px)))];
    assert.ok(ends.length >= 11 && ends.length <= 12, `${ends.length} views`);
    for (let i = 1; i < ends.length; i++) assert.ok(Math.abs(ends[i] - ends[i - 1] - step) < 1e-6);
  });

  test("now stays on the same pixel of the view", () => {
    for (const t of times) assert.equal(nowPixel(t, liveViewEnd(t, span, px)), px - Math.round(px * 0.05));
  });
});

// ---- controls and preferences ---------------------------------------------------------

describe("sliderKey", () => {
  const range = { min: 1000, max: 2000, step: 10 };

  test("arrows step, Page Up / Down take ten steps, Home / End the ends", () => {
    assert.equal(sliderKey("ArrowRight", 1500, range), 1510);
    assert.equal(sliderKey("ArrowUp", 1500, range), 1510);
    assert.equal(sliderKey("ArrowLeft", 1500, range), 1490);
    assert.equal(sliderKey("ArrowDown", 1500, range), 1490);
    assert.equal(sliderKey("PageUp", 1500, range), 1600);
    assert.equal(sliderKey("PageDown", 1500, { ...range, page: 250 }), 1250);
    assert.equal(sliderKey("Home", 1500, range), 1000);
    assert.equal(sliderKey("End", 1500, range), 2000);
  });

  test("never past the ends", () => {
    assert.equal(sliderKey("ArrowRight", 1995, range), 2000);
    assert.equal(sliderKey("PageDown", 1020, range), 1000);
  });

  test("other keys: null (left to the page)", () => {
    for (const key of ["Tab", "Enter", " ", "a"]) assert.equal(sliderKey(key, 1500, range), null);
  });
});

describe("setSliderValue", () => {
  const fakeSlider = () => ({
    attrs: {},
    writes: 0,
    getAttribute(k) {
      return this.attrs[k] ?? null;
    },
    setAttribute(k, v) {
      this.writes++;
      this.attrs[k] = v;
    },
  });

  test("the value within the range, rounded, and what it says", () => {
    const s = fakeSlider();
    setSliderValue(s, 1500.4, "12:25:00", 1000, 2000);
    assert.deepEqual(s.attrs, { "aria-valuenow": "1500", "aria-valuetext": "12:25:00" });
    setSliderValue(s, 2500, "12:41:40", 1000, 2000);
    assert.equal(s.attrs["aria-valuenow"], "2000");
  });

  test("the same text after the range moved (a pan while paused): the value moves into it", () => {
    const s = fakeSlider();
    setSliderValue(s, 1500, "12:25:00", 1000, 2000);
    setSliderValue(s, 1500, "12:25:00", 1800, 2800);
    assert.equal(s.attrs["aria-valuenow"], "1800");
  });

  test("nothing changed: nothing written", () => {
    const s = fakeSlider();
    setSliderValue(s, 1500, "12:25:00", 1000, 2000);
    const writes = s.writes;
    setSliderValue(s, 1500.2, "12:25:00", 900, 2100);
    assert.equal(s.writes, writes);
  });
});

describe("kindTest", () => {
  test("none chosen: everything", () => {
    assert.equal(kindTest(new Set())("anything"), true);
  });

  test("any of a bookmark's kinds, whatever the case and spacing", () => {
    const ok = kindTest(new Set(["Car", "dog"]));
    assert.equal(ok("Person, car"), true);
    assert.equal(ok(" Dog "), true);
    assert.equal(ok("Person"), false);
    assert.equal(ok("Carport"), false);
    assert.equal(ok(null), false);
  });
});

describe("labels", () => {
  test("labelAttrs names a control twice, escaped", () => {
    assert.equal(labelAttrs(`Tom's "cam" <1>`), 'title="Tom&#39;s &quot;cam&quot; &lt;1&gt;" aria-label="Tom&#39;s &quot;cam&quot; &lt;1&gt;"');
    assert.equal(esc("a & b"), "a &amp; b");
  });
});

describe("prefsFor", () => {
  const stored = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const useStorage = (storage) => Object.defineProperty(globalThis, "localStorage", { value: storage, configurable: true });
  afterEach(() => {
    if (stored) Object.defineProperty(globalThis, "localStorage", stored);
    else delete globalThis.localStorage;
  });

  test("values round-trip as JSON under the card's prefix", () => {
    const map = new Map();
    useStorage({ getItem: (k) => map.get(k) ?? null, setItem: (k, v) => map.set(k, String(v)) });
    const prefs = prefsFor("ss-timeline-card.");
    prefs.set("kinds", ["Car"]);
    assert.deepEqual(map.get("ss-timeline-card.kinds"), '["Car"]');
    assert.deepEqual(prefs.get("kinds", []), ["Car"]);
    assert.equal(prefsFor("ss-timelapse:").get("kinds", null), null);
    map.set("ss-timeline-card.grid", "not json");
    assert.equal(prefs.get("grid", true), true);
  });

  test("storage unavailable: the fallback, and nothing thrown", () => {
    useStorage({ getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("denied"); } });
    const prefs = prefsFor("ss-timelapse:");
    assert.equal(prefs.get("span", 3600), 3600);
    assert.doesNotThrow(() => prefs.set("span", 900));
  });
});

describe("the veil", () => {
  // Just enough of an element for setVeil.
  const fakeVeil = () => {
    const classes = new Set(["off"]);
    const parts = { "ha-icon": { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } }, ".vtext": {}, ".vsub": {} };
    return {
      classes,
      parts,
      classList: {
        toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
        add: (c) => classes.add(c),
        remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      },
      querySelector: (q) => parts[q],
    };
  };

  test("loading says so with an ellipsis; an error its icon; no text hides it", () => {
    const v = fakeVeil();
    setVeil(v, "Loading", "loading", "Drive Way");
    assert.deepEqual([...v.classes], ["loading"]);
    assert.equal(v.parts[".vtext"].textContent, "Loading…");
    assert.equal(v.parts[".vsub"].textContent, "Drive Way");
    setVeil(v, "Playback failed", "error");
    assert.deepEqual([...v.classes], ["error"]);
    assert.equal(v.parts["ha-icon"].attrs.icon, "mdi:alert-circle-outline");
    assert.equal(v.parts[".vtext"].textContent, "Playback failed");
    setVeil(v, "");
    assert.ok(v.classes.has("off"));
  });

  test("Retry only where asked for", () => {
    assert.match(veilHtml(true), /data-act="retry"/);
    assert.doesNotMatch(veilHtml(), /button/);
  });
});
