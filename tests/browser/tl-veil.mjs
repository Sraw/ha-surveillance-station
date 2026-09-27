// The time-lapse veil on a phone: "Loading…" and the day while a day opens (never
// the WebView's big play button), gone once playing; a paused seek to a spot not
// fetched yet clears it once the frame is there. Exits 1 if a check fails.
import { open, sleep } from "./harness.mjs";
const { browser, page, ev } = await open({ w: 412, h: 915, mobile: true, path: "/ss-playback/timelapse", tag: "ss-timelapse-card" });
let failed = 0;
const check = (ok, what) => { console.log(`${ok ? "ok  " : "FAIL"} ${what}`); if (!ok) failed++; };
const veil = () => ev((c) => c._veil.classList.contains("off") ? "" : c._veil.textContent.trim().replace(/\s+/g, " "));
const until = async (fn, ms = 20000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (await ev(fn)) return Date.now() - t0; await sleep(50); } return -1; };
await until((c) => c._video.currentTime > 0.2);
check(await ev((c) => c._video.poster.startsWith("data:image/gif")), "blank poster (no WebView play button)");

// A day unlikely to be cached: Drive Way's oldest. (Not Front Door: its time-lapse hangs
// this installation's Iris Xe when decoded on it, and Frigate with it.)
const date = await ev((c) => { const cam = c._index.cameras.find((x) => x.name === "Drive Way"); c._selectCamera(cam.id); return cam.days.at(-1).date; });
await sleep(300);
await ev((c, d) => c._selectDay(d), date);
check(await ev((c, d) => c._date === d, date), `opened ${date}`);
let seen = "";
const t0 = Date.now();
while (Date.now() - t0 < 4000 && !seen && !(await ev((c) => c._video.currentTime > 0.2))) { seen = await veil(); await sleep(50); }
if (seen) {
  await page.screenshot({ path: "tl-loading.png" });
  check(seen.startsWith("Loading…"), `veil while loading: "${seen}"`);
} else console.log(`skip veil check: the day opened within ${Date.now() - t0} ms`);
check((await until((c) => c._video.currentTime > 0.2)) >= 0 && (await veil()) === "", "veil gone once playing");

// Paused, scrub to 09:00 (not fetched yet): Loading, then the paused frame without a veil.
await ev((c) => { c._video.pause(); c._video.currentTime = c._mediaAt(c._day.start + 9 * 3600); });
const ms = await until((c) => c._video.readyState >= 2 && !c._video.seeking);
await sleep(600);
check(ms >= 0 && (await veil()) === "" && (await ev((c) => c._video.paused)), `paused seek: frame after ${ms} ms, no veil left ("${await veil()}")`);
await browser.close(); process.exit(failed ? 1 : 0);
