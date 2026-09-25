// Races: making a follower master while it holds its first frame; Live,
// then a past time, then Live again before anything landed.
import { open, sleep } from "./harness.mjs";
const { browser, ev } = await open({ prefs: {} });
await sleep(6000);
const state = () => ev((c) => [...c._players.values()].map((p) => `${c._master === p ? "*" : ""}${p.cameraId}:${p.feed?.live ? "live" : "rec"}${p.video.paused ? "P" : ">"}${p.loading ? "L" : ""}`).join(" ") + " icon=" + c.shadowRoot.querySelector('[data-act="play"] ha-icon').getAttribute("icon"));
const T = Math.floor(Date.now() / 1000) - 5400;
await ev((c, T) => c._seekAll(T, true), T);
await sleep(900); // followers landed, holding for the master
console.log("before:", await state());
await ev((c) => c._setMaster(c._shown[1]));
for (let i = 1; i <= 16; i++) { await sleep(500); console.log(`held follower made master, +${i}s:`, await state(), await ev((c) => { const p = c._players.get(c._shown[0]), f = p.feed, m = c._master.wall(); return `old master ${p.cameraId}: d${(p.wall() - m).toFixed(2)} held${p.held ? 1 : 0} lead${p.lead} edge${((f.lastWall ?? 0) - m).toFixed(2)} seek${f.seeking ? 1 : 0} land${f.wantLanding ? "w" : f.landing ? "y" : "-"} ll${((Date.now() - p.lastLoad.at) / 1000).toFixed(1)} rs${p.video.readyState} ahead${(f.track.sink.end() - p.video.currentTime).toFixed(2)} ssP${f.ssPaused ? 1 : 0} gap${p.gap ? 1 : 0}`; })); }
await ev((c) => c.shadowRoot.querySelector('[data-act="live"]').click());
await sleep(50);
await ev((c, T) => c._seekAll(T, true), T + 600);
await sleep(100);
await ev((c) => c.shadowRoot.querySelector('[data-act="live"]').click());
await sleep(5000);
console.log("Live, past, Live:", await state());
await browser.close(); process.exit(0);
