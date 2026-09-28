// Notification links: ?ss_camera=&ss_time= on load, and opened while the
// card is already on screen (HA navigates in place: location-changed).
import { open, sleep } from "./harness.mjs";
const t0 = Math.floor(Date.now() / 1000) - 600;
const { browser, page, card, ev } = await open({ prefs: { grid: false }, path: `/ss-playback/playback?ss_camera=Drive%20Way&ss_time=${t0}` });
let failed = false;
const check = async (label, cam, t) => {
  let s;
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    s = await ev((c) => ({ cam: c._leader?.cameraId, wall: c._leader?.wall(), playing: !c._leader?.video.paused }));
    if (s.cam === cam && s.playing && Math.abs(s.wall - t) < 5) break;
  }
  const ok = s.cam === cam && s.playing && Math.abs(s.wall - t) < 5;
  if (!ok) failed = true;
  console.log(ok ? "ok  " : "FAIL", label, `camera ${s.cam}, at ${(s.wall - t).toFixed(1)}s from the link, ${s.playing ? "playing" : "paused"}`);
};
await check("on load", 6, t0);
const gone = async (label) => {
  const q = await page.evaluate(() => location.search);
  const ok = !q.includes("ss_");
  if (!ok) failed = true;
  console.log(ok ? "ok  " : "FAIL", `${label}: link taken out of the address (${q || "empty"})`);
};
await gone("on load");
// Opened in place: the app pushes the new address and tells the frontend.
const nav = (url) => page.evaluate((url) => {
  history.pushState(null, "", url);
  window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
}, url);
const t1 = t0 - 1800;
await nav(`/ss-playback/playback?ss_camera=10&ss_time=${t1}`);
await check("in place", 10, t1);
await gone("in place");
// The same link again, a while later (a second tap): back to that moment.
await ev((c) => c._leader.seek(c._leader.wall() + 120, true));
await sleep(3000);
await nav(`/ss-playback/playback?ss_camera=10&ss_time=${t1}`);
await check("same link again", 10, t1);
// Going back (or a dialog closing, which HA does with history.back) doesn't
// return to the link.
await ev((c) => c._leader.seek(c._leader.wall() + 300, true));
await sleep(3000);
const b0 = await ev((c) => c._leader.wall());
await page.evaluate(() => history.back());
await sleep(2500);
const b1 = await ev((c) => c._leader.wall());
const kept = b1 - b0 > 0 && b1 - b0 < 5;
if (!kept) failed = true;
console.log(kept ? "ok  " : "FAIL", `back: kept playing where it was (${(b1 - b0).toFixed(1)}s on)`);
// Other navigation (no link) leaves playback alone.
const before = await ev((c) => c._leader.wall());
await nav(`/ss-playback/playback`);
await sleep(2000);
const after = await ev((c) => c._leader.wall());
const ok = after - before > 0 && after - before < 4;
if (!ok) failed = true;
console.log(ok ? "ok  " : "FAIL", `plain navigation: kept playing (${(after - before).toFixed(1)}s on)`);
await browser.close(); process.exit(failed ? 1 : 0);
