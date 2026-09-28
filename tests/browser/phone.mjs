// Phone-sized: a notification-style deep link (?ss_camera&ss_time), then an
// event tap. `node phone.mjs nomse` hides MSE: the native HLS path. Exits 1 if
// a check fails.
import { open, snap, sleep, checks } from "./harness.mjs";
const noMse = process.argv[2] === "nomse";
const T = 1790293258; // bookmark "push test"
const o = await open({ w: 390, h: 844, mobile: true, path: `/ss-playback/playback?ss_camera=6&ss_time=${T}` });
const { browser, page, ev } = o;
const { check, done } = checks();
if (noMse) await page.addInitScript(() => { delete window.MediaSource; delete window.ManagedMediaSource; });
if (noMse) { await page.reload(); await page.locator("ss-timeline-card").waitFor({ state: "attached" }); }
await sleep(5000);
const at = () => ev((c) => ({ cam: c._master?.cameraId, wall: c._master?.wall(), playing: !c._master?.video.paused }));
const mse = await page.evaluate(() => !!window.MediaSource);
console.log("deep link:", await snap(ev, T + 5), "MSE", mse);
check(mse === !noMse, noMse ? "MSE hidden: the HLS path" : "MSE there");
// Playing from the link's time (a jump lands up to a keyframe before it; HLS loads slower).
let s = await at();
check(s.cam === 6 && s.playing && s.wall > T - 2 && s.wall < T + 10, `deep link: camera 6 plays from the link (${(s.wall - T).toFixed(1)} s on)`);
const evs = await ev((c) => c._evItems.slice(0, 3).map((e) => e.id));
await ev((c, id) => c.shadowRoot.querySelector(`[data-ev="${id}"]`).click(), evs[1]);
const e1 = await ev((c, id) => c._evItems.find((x) => x.id === id), evs[1]);
await sleep(4000);
console.log("event tap:", await snap(ev, e1.start + 1));
// An event plays its camera from 3 s before it.
s = await at();
check(s.cam === e1.camera_id && s.playing && s.wall > e1.start - 5 && s.wall < e1.start + 5, `event tap: its camera plays from just before it (${(s.wall - e1.start).toFixed(1)} s)`);
await page.screenshot({ path: noMse ? "pb6-nomse.png" : "pb6.png" });
await done(browser);
