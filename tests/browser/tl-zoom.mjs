// The time-lapse card's timeline zoom, pan and skip buttons; the playhead goes
// where it was sent at once, however long the segment there takes to load
// (network throttled). Exits 1 if any check fails.
import { open, sleep } from "./harness.mjs";
const mobile = !!process.env.MOBILE;
const { browser, page, ev } = await open({ path: "/ss-playback/timelapse", tag: "ss-timelapse-card", ...(mobile ? { w: 412, h: 860, mobile: true } : {}) });
let failed = 0;
const check = (ok, what) => { console.log(`${ok ? "ok  " : "FAIL"} ${what}`); if (!ok) failed++; };
const until = async (fn, ms = 20000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (await ev(fn)) return Date.now() - t0; await sleep(50); } return -1; };
const head = () => ev((c) => ({ f: parseFloat(c._headEl.style.left) / 100, shown: c._headEl.style.display !== "none", wall: c._wallAt(c._video.currentTime), view: c._view, t: c._video.currentTime }));
const btn = (sel) => ev((c, sel) => c.shadowRoot.querySelector(sel).click(), sel);

check((await until((c) => c._video.currentTime > 0.2)) > 0, "plays");
await ev((c) => c._stepDay(1)); // a finished day
check((await until((c) => c._session && c._video.currentTime > 0.2)) > 0, "previous day plays");
// All four zoom levels are there; 24h shows the day.
check(await ev((c) => c.shadowRoot.querySelectorAll("[data-span]").length === 4), "four zoom levels");
await btn('[data-span="86400"]');
check(await ev((c) => c._view.end - c._view.start === c._day.end - c._day.start), "24h shows the whole day");
// Seek to noon, zoom to 1h: an hour around the playhead, head inside, ticks and range shown.
await ev((c) => { c._video.currentTime = c._mediaAt(c._day.start + 12 * 3600); });
await sleep(1500);
await btn('[data-span="3600"]');
let h = await head();
check(h.view.end - h.view.start === 3600 && h.wall >= h.view.start && h.wall <= h.view.end && h.shown, `1h view around the playhead (head at ${(h.f * 100).toFixed(0)}%)`);
const ticks = await ev((c) => [...c.shadowRoot.querySelectorAll(".ticks span")].map((s) => s.textContent));
check(ticks.length >= 2 && ticks.every((t) => /^\d\d:\d\d$/.test(t)), `ticks ${ticks.join(" ")}`);
const range = await ev((c) => getComputedStyle(c.shadowRoot.querySelector(".range")).display === "none" ? "(hidden)" : c.shadowRoot.querySelector(".range").textContent);
console.log("range:", range);
// A click on the zoomed bar lands at that wall time, precisely.
await ev((c) => c._video.pause());
const box = await ev((c) => { const r = c._scrub.getBoundingClientRect(); return [r.left, r.top, r.width]; });
const want = await ev((c) => c._view.start + 0.75 * (c._view.end - c._view.start));
await page.mouse.click(box[0] + box[2] * 0.75, box[1] + 10);
await sleep(300);
h = await head();
check(Math.abs(h.wall - want) < 60, `a click at 75% of the 1h view lands within a minute (off by ${(h.wall - want).toFixed(0)} s)`);

// Slow loads from here: the playhead moves first, the load follows. And
// currentTime as WebKit reports it: the old time until a seek has landed.
await ev((c) => {
  const v = c._video, real = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "currentTime");
  let settled = real.get.call(v);
  Object.defineProperty(v, "currentTime", { configurable: true, set(t) { real.set.call(this, t); },
    get() { return this.seeking || Date.now() < this._staleUntil ? settled : (settled = real.get.call(this)); } });
  // ...and even for a second past "seeked" (as a phone's decoder might).
  v.addEventListener("seeked", () => (v._staleUntil = Date.now() + 1000));
});
// Where the head is drawn, as a wall time.
const drawn = () => ev((c) => c._view.start + (parseFloat(c._headEl.style.left) / 100) * (c._view.end - c._view.start));
const cdp = await page.context().newCDPSession(page);
await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 300, downloadThroughput: 150_000, uploadThroughput: 100_000 });
for (const [sel, d] of [['[data-skip="30"]', 30], ['[data-skip="10"]', 10], ['[data-skip="-10"]', -10]]) {
  await ev((c) => c._video.pause());
  const from = await ev((c) => c._seekTarget ?? c._video.currentTime);
  const want = await ev((c, t) => c._wallAt(t), from + d);
  await btn(sel);
  const seen = [];
  for (let i = 0; i < 20; i++) { seen.push(await drawn()); await sleep(100); }
  const off = seen.map((w) => Math.abs(w - want));
  check(off.every((o) => o < 30), `${sel}: the head is at the target from the click on, through the load (worst ${Math.max(...off).toFixed(0)} s off; seeking ${await ev((c) => c._video.seeking)})`);
}
const far = await ev((c) => c._mediaAt(c._day.start + 20 * 3600));
const wantFar = await ev((c) => c._day.start + 20 * 3600);
await btn('[data-span="86400"]');
await ev((c, t) => c._seek(t), far);
const seenFar = [];
for (let i = 0; i < 20; i++) { seenFar.push(await drawn()); await sleep(100); }
check(seenFar.every((w) => Math.abs(w - wantFar) < 300), `a far seek (20:00): the head is there at once and stays (worst ${Math.max(...seenFar.map((w) => Math.abs(w - wantFar))).toFixed(0)} s off)`);
// Zoomed right in, a short hop (a fraction of a video second): the head stays at the target too.
await btn('[data-span="900"]');
await ev((c) => c._video.pause());
await sleep(500);
const zb = await ev((c) => { const r = c._scrub.getBoundingClientRect(); return [r.left, r.top, r.width]; });
const wantNear = await ev((c) => c._view.start + 0.62 * (c._view.end - c._view.start));
await page.mouse.click(zb[0] + zb[2] * 0.62, zb[1] + 10);
const seenNear = [];
for (let i = 0; i < 25; i++) { seenNear.push(await drawn()); await sleep(100); }
check(seenNear.every((w) => Math.abs(w - wantNear) < 20), `a short hop on the 15m view: the head stays at the target (worst ${Math.max(...seenNear.map((w) => Math.abs(w - wantNear))).toFixed(0)} s off)`);
await btn('[data-span="3600"]');
const loaded = await until((c) => !c._video.seeking && c._seekTarget == null, 90000);
console.log(`throttled load finished after ${loaded} ms`);
await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });

// Pan: an hour later, and the view stays there for a while though the head is elsewhere.
const v0 = (await head()).view;
await btn('[data-pan="1"]');
const v1 = (await head()).view;
check(Math.abs(v1.start - v0.start - 1800) < 1, `pan later moves half the view (${((v1.start - v0.start) / 60).toFixed(0)} min)`);
await ev((c) => c._play());
await sleep(1500);
check(Math.abs((await head()).view.start - v1.start) < 1, "the view stays where it was panned");
// A click on the bar after a pan: the view follows the playhead again at once.
await ev((c) => c._video.pause());
const pb = await ev((c) => { const r = c._scrub.getBoundingClientRect(); return [r.left, r.top, r.width]; });
await page.mouse.click(pb[0] + pb[2] * 0.97, pb[1] + 10);
await until((c) => c._seekTarget == null && !c._video.seeking, 30000);
await ev((c) => c._play());
await sleep(4000); // 1 s of video: 4 min, past the end of the 1h view from 97%
h = await head();
check(h.shown && h.wall >= h.view.start && h.wall <= h.view.end, `after a pan, a click on the bar: the view follows the playhead past its end (head at ${(h.f * 100).toFixed(0)}%)`);
// Back to 24h.
await btn('[data-span="86400"]');
check(await ev((c) => c._view.end - c._view.start === c._day.end - c._day.start && [...c.shadowRoot.querySelectorAll("[data-pan]")].every((b) => b.disabled)), "24h again: whole day, no panning");
const fit = await ev((c) => { const r = c.shadowRoot.querySelector(".tlrow").getBoundingClientRect(); return [Math.round(r.bottom), innerHeight]; });
check(fit[0] <= fit[1], `timeline ends at ${fit[0]} px of a ${fit[1]} px window`);
await page.screenshot({ path: mobile ? "tl-zoom-phone.png" : "tl-zoom.png" });
await browser.close();
process.exit(failed ? 1 : 0);
