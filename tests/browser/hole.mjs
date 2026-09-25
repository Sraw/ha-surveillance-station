// A hole inside a recording file (SS sends nothing for that long): the card
// asks SS for 16x until frames come again. Pass the camera and the time the
// hole starts: node hole.mjs <camera id> <epoch seconds>.
import { open, snap, sleep } from "./harness.mjs";
const [cam, t] = process.argv.slice(2).map(Number);
const { browser, ev } = await open({ prefs: { cameras: [cam] } });
await sleep(5000);
await ev((c, t) => c._seekAll(t, true), t - 6);
for (let i = 0; i < 12; i++) { await sleep(1000); console.log(`+${i + 1}s`, new Date((await ev((c) => c._master.wall())) * 1000).toISOString().slice(11, 21), await snap(ev, 0)); }
await browser.close(); process.exit(0);
