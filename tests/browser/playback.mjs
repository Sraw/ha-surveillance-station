// One camera: seek to just before a recording-file boundary and play across
// it, skips (within the buffer and over the socket), 4x / 8x / 1x, pause and
// resume (SS paused by flow control while the card is).
import { open, snap, sleep } from "./harness.mjs";
const { browser, page, ev } = await open({ prefs: { cameras: [6] } });
await sleep(5000);
const recs = await ev((c) => c._ws({ type: "surveillance_station/recordings", camera_id: 6, start: Math.floor(Date.now() / 1000) - 6 * 3600, end: Math.floor(Date.now() / 1000) }));
const starts = recs.recordings.map((r) => r.start).sort((a, b) => a - b);
const B = starts[starts.length - 3]; // a file boundary
console.log("boundary at", new Date(B * 1000).toISOString(), "files", starts.length);
const wall = () => ev((c) => c._master.wall());
async function step(label, fn, arg, secs = 3, expect = null) {
  const t0 = Date.now();
  await ev(fn, arg);
  let landed = null;
  for (let i = 0; i < secs * 4; i++) {
    await sleep(250);
    const s = await ev((c) => ({ loading: c._master.loading, w: c._master.wall(), p: c._master.video.paused }));
    if (landed == null && !s.loading && !s.p) landed = (Date.now() - t0) / 1000;
  }
  const w = await wall();
  console.log(`${label}: playing after ${landed}s; wall ${expect != null ? "vs expected " + (w - expect - (Date.now() - t0) / 1000).toFixed(2) : new Date(w * 1000).toISOString()}`);
  console.log("   ", await snap(ev, w));
}
const T = B - 8;
await step("seek to boundary-8s", (c, T) => c._seekAll(T, true), T, 3, T);
let w0 = await wall(); let t0 = Date.now();
await sleep(10000);
console.log("across boundary: wall advanced", ((await wall()) - w0).toFixed(2), "in", ((Date.now() - t0) / 1000).toFixed(2), "s", await snap(ev, await wall()));
for (const d of [-10, 10, 30, -30]) {
  const before = await wall();
  await step(`skip ${d}`, (c, d) => c.shadowRoot.querySelector(`[data-skip="${d}"]`).click(), d, 2, before + d);
}
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "4"; s.dispatchEvent(new Event("change")); });
w0 = await wall(); t0 = Date.now(); await sleep(6000);
console.log("speed 4: footage/s", (((await wall()) - w0) / ((Date.now() - t0) / 1000)).toFixed(2), await snap(ev, await wall()));
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "8"; s.dispatchEvent(new Event("change")); });
await sleep(2000); w0 = await wall(); t0 = Date.now(); await sleep(6000);
console.log("speed 8: footage/s", (((await wall()) - w0) / ((Date.now() - t0) / 1000)).toFixed(2), await snap(ev, await wall()));
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "1"; s.dispatchEvent(new Event("change")); });
await sleep(2000); w0 = await wall(); t0 = Date.now(); await sleep(5000);
console.log("speed 1: footage/s", (((await wall()) - w0) / ((Date.now() - t0) / 1000)).toFixed(2), await snap(ev, await wall()));
await ev((c) => c.shadowRoot.querySelector('[data-act="play"]').click());
await sleep(15000);
const wp = await wall();
console.log("paused 15s:", await snap(ev, wp));
await ev((c) => c.shadowRoot.querySelector('[data-act="play"]').click());
await sleep(3000);
console.log("resumed 3s: advanced", ((await wall()) - wp).toFixed(2), await snap(ev, await wall()));
await browser.close(); process.exit(0);
