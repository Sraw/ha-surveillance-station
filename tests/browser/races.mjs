// Races: making a follower master while it holds its first frame; Live,
// then a past time, then Live again before anything landed. Exits 1 if the
// grid doesn't end up playing, all of it, where it was last sent.
import { open, sleep, checks } from "./harness.mjs";
const { browser, ev } = await open({ prefs: {} });
const { check, done } = checks();
await sleep(6000);
const state = () => ev((c) => [...c._players.values()].map((p) => `${c._master === p ? "*" : ""}${p.cameraId}:${p.feed?.live ? "live" : "rec"}${p.video.paused ? "P" : ">"}${p.loading ? "L" : ""}`).join(" ") + " icon=" + c.shadowRoot.querySelector('[data-act="play"] ha-icon').getAttribute("icon"));
// Every camera on the stream asked for (live or not), playing (or in a gap of its own), none loading, and the play button says so.
const settled = (live) => ev((c, live) => [...c._players.values()].every((p) => !!p.feed?.live === live && (p.gap || !p.video.paused) && !p.loading) &&
  c.shadowRoot.querySelector('[data-act="play"] ha-icon').getAttribute("icon") === "mdi:pause", live);
const T = Math.floor(Date.now() / 1000) - 5400;
await ev((c, T) => c._seekAll(T, true), T);
await sleep(900); // followers landed, holding for the master
console.log("before:", await state());
await ev((c) => c._setMaster(c._shown[1]));
for (let i = 1; i <= 16; i++) { await sleep(500); console.log(`held follower made master, +${i}s:`, await state(), await ev((c) => { const p = c._players.get(c._shown[0]), f = p.feed, m = c._master.wall(); return `old master ${p.cameraId}: d${(p.wall() - m).toFixed(2)} held${p.held ? 1 : 0} lead${p.lead} edge${((f.lastWall ?? 0) - m).toFixed(2)} seek${f.seeking ? 1 : 0} land${f.wantLanding ? "w" : f.landing ? "y" : "-"} ll${((Date.now() - p.lastLoad.at) / 1000).toFixed(1)} rs${p.video.readyState} ahead${(f.track.sink.end() - p.video.currentTime).toFixed(2)} ssP${f.ssPaused ? 1 : 0} gap${p.gap ? 1 : 0}`; })); }
check(await settled(false), "a held follower made master: every camera plays the recordings");
check(Math.abs((await ev((c) => c._master.wall())) - T - 9) < 5, "near where it was sent (+8 s of playing)");
await ev((c) => c.shadowRoot.querySelector('[data-act="live"]').click());
await sleep(50);
await ev((c, T) => c._seekAll(T, true), T + 600);
await sleep(100);
await ev((c) => c.shadowRoot.querySelector('[data-act="live"]').click());
await sleep(5000);
console.log("Live, past, Live:", await state());
check(await settled(true), "Live, a past time, Live: every camera live and playing");
await done(browser);
