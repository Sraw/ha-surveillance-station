// A hole inside a recording file (SS sends nothing for that long): the card
// asks SS for 16x until frames come again. Pass the camera and the time the
// hole starts: node hole.mjs <camera id> <epoch seconds>. Exits 1 if, 12 s
// after starting 6 s before it, playback isn't well past it (unbridged, a
// hole longer than 5 s would still be playing out).
import { open, snap, sleep, checks } from "./harness.mjs";
const [cam, t] = process.argv.slice(2).map(Number);
const { browser, ev } = await open({ prefs: { cameras: [cam] } });
const { check, done } = checks();
await sleep(5000);
await ev((c, t) => c._seekAll(t, true), t - 6);
const walls = [];
for (let i = 0; i < 12; i++) { await sleep(1000); walls.push(await ev((c) => c._master.wall())); console.log(`+${i + 1}s`, new Date(walls.at(-1) * 1000).toISOString().slice(11, 21), await snap(ev, 0)); }
check(walls.at(-1) > t + 5, `past the hole: ${(walls.at(-1) - t).toFixed(1)} s after its start`);
check(walls.at(-1) - walls.at(-2) > 0.5, "and playing");
await done(browser);
