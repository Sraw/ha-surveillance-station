// One camera: seek to just before a recording-file boundary and play across
// it, skips (within the buffer and over the socket), 4x / 8x / 1x, pause and
// resume (SS paused by flow control while the card is). Exits 1 if a check fails.
import { open, snap, sleep, checks } from "./harness.mjs";
const { browser, page, ev } = await open({ prefs: { cameras: [6] } });
const { check, done } = checks();
await sleep(5000);
const recs = await ev((c) => c._ws({ type: "surveillance_station/recordings", camera_id: 6, start: Math.floor(Date.now() / 1000) - 6 * 3600, end: Math.floor(Date.now() / 1000) }));
const starts = recs.recordings.map((r) => r.start).sort((a, b) => a - b);
const B = starts[starts.length - 3]; // a file boundary
console.log("boundary at", new Date(B * 1000).toISOString(), "files", starts.length);
const wall = () => ev((c) => c._leader.wall());
// A jump lands on the keyframe before the time (a GOP, ~1 s here), after up to ~1 s over the socket.
async function step(label, fn, arg, secs = 3, expect = null) {
  const t0 = Date.now();
  await ev(fn, arg);
  let landed = null;
  for (let i = 0; i < secs * 4; i++) {
    await sleep(250);
    const s = await ev((c) => ({ loading: c._leader.loading, w: c._leader.wall(), p: c._leader.video.paused }));
    if (landed == null && !s.loading && !s.p) landed = (Date.now() - t0) / 1000;
  }
  const w = await wall();
  const off = expect != null ? w - expect - (Date.now() - t0) / 1000 : null;
  console.log(`${label}: playing after ${landed}s; wall ${expect != null ? "vs expected " + off.toFixed(2) : new Date(w * 1000).toISOString()}`);
  console.log("   ", await snap(ev, w));
  check(landed != null && (off == null || (off > -3 && off < 1)), `${label}: plays, where it was sent`);
}
const footage = async (label, lo, hi, settle = 2000) => {
  await sleep(settle); // the frames still coming were sent at the old speed
  const w0 = await wall(), t0 = Date.now();
  await sleep(6000);
  const rate = ((await wall()) - w0) / ((Date.now() - t0) / 1000);
  console.log(`${label}: footage/s`, rate.toFixed(2), await snap(ev, await wall()));
  check(rate > lo && rate < hi, `${label}: ${rate.toFixed(2)} s of footage a second`);
};
const speed = (v) => ev((c, v) => { const s = c.shadowRoot.querySelector(".speed"); s.value = v; s.dispatchEvent(new Event("change")); }, v);
const T = B - 8;
await step("seek to boundary-8s", (c, T) => c._seekAll(T, true), T, 3, T);
let w0 = await wall(); let t0 = Date.now();
await sleep(10000);
const across = (await wall()) - w0, dt = (Date.now() - t0) / 1000;
console.log("across boundary: wall advanced", across.toFixed(2), "in", dt.toFixed(2), "s", await snap(ev, await wall()));
check(Math.abs(across - dt) < 1.5, "plays across the recording-file boundary at 1x");
for (const d of [-10, 10, 30, -30]) {
  const before = await wall();
  await step(`skip ${d}`, (c, d) => c.shadowRoot.querySelector(`[data-skip="${d}"]`).click(), d, 2, before + d);
}
await speed("4");
await footage("speed 4", 3, 5);
await speed("8");
await footage("speed 8", 6, 10);
await speed("1");
await footage("speed 1", 0.8, 1.2);
await ev((c) => c.shadowRoot.querySelector('[data-act="play"]').click());
await sleep(1000);
const w1 = await wall();
await sleep(14000);
const wp = await wall();
console.log("paused 15s:", await snap(ev, wp));
const held = await ev((c) => ({ paused: c._leader.video.paused, ss: !!c._leader.feed?.ssPaused }));
check(held.paused && Math.abs(wp - w1) < 0.3, "paused: the picture holds");
check(held.ss, "paused: SS told to pause (flow control)");
await ev((c) => c.shadowRoot.querySelector('[data-act="play"]').click());
await sleep(3000);
const resumed = (await wall()) - wp;
console.log("resumed 3s: advanced", resumed.toFixed(2), await snap(ev, await wall()));
check(resumed > 1.5 && resumed < 3.5, "resumes from where it paused");
await done(browser);
