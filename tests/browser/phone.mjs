// Phone-sized: a notification-style deep link (?ss_camera&ss_time), then an
// event tap. `node phone.mjs nomse` hides MSE: the native HLS path.
import { open, snap, sleep } from "./harness.mjs";
const noMse = process.argv[2] === "nomse";
const T = 1790293258; // bookmark "push test"
const o = await open({ w: 390, h: 844, mobile: true, path: `/ss-playback/playback?ss_camera=6&ss_time=${T}` });
const { browser, page, ev } = o;
if (noMse) await page.addInitScript(() => { delete window.MediaSource; delete window.ManagedMediaSource; });
if (noMse) { await page.reload(); await page.locator("ss-timeline-card").waitFor({ state: "attached" }); }
await sleep(5000);
console.log("deep link:", await snap(ev, T + 5), "MSE", await page.evaluate(() => !!window.MediaSource));
const evs = await ev((c) => c._evItems.slice(0, 3).map((e) => e.id));
await ev((c, id) => c.shadowRoot.querySelector(`[data-ev="${id}"]`).click(), evs[1]);
const e1 = await ev((c, id) => c._evItems.find((x) => x.id === id).start, evs[1]);
await sleep(4000);
console.log("event tap:", await snap(ev, e1 + 1));
await page.screenshot({ path: noMse ? "pb6-nomse.png" : "pb6.png" });
await browser.close(); process.exit(0);
