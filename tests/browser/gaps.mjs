// Recording gaps in a grid: a follower with nothing recorded shows "No
// recording" until the master reaches its next footage; a master sent into
// its own gap lands on its next footage and takes the others there; a gap
// every camera shares is skipped over. The gaps are found in the last week.
import { open, sleep } from "./harness.mjs";
const { browser, ev } = await open({ prefs: {} });
await sleep(6000);
const st = async (label) => console.log(label.padEnd(10), await ev((c) => {
  const m = c._master.wall();
  return new Date(m * 1000).toISOString().slice(11, 19) + " | " + [...c._players.values()].map((p) => `${c._master === p ? "*" : ""}${p.cameraId}:${(p.wall() - m).toFixed(1)}${p.video.paused ? "P" : ""}${p.gap ? "G" : ""}${p.loading ? "L" : ""}${p.veilKind ? "[" + p.veil.textContent.trim().replace(/\s+/g, " ").slice(0, 20) + "]" : ""}`).join(" ");
}));
const gaps = await ev(async (c) => {
  const now = Math.floor(Date.now() / 1000);
  const out = {};
  for (const id of c._shown) {
    const recs = (await c._ws({ type: "surveillance_station/recordings", camera_id: id, start: now - 7 * 86400, end: now })).recordings.sort((a, b) => a.start - b.start);
    out[id] = recs.slice(1).map((r, i) => [recs[i].end, r.start]).filter(([a, b]) => b - a > 10);
  }
  return out;
});
const ids = Object.keys(gaps).map(Number);
const covered = (id, t) => !gaps[id].some(([a, b]) => t >= a - 15 && t < b + 15);
const shared = gaps[ids[0]].find(([a]) => ids.every((id) => gaps[id].some(([x, y]) => x < a + 5 && y > a + 5)));
// A camera's own gap of at least a minute while another has footage all through.
let own = null;
for (const id of ids) for (const [a, b] of gaps[id]) {
  const other = ids.find((o) => o !== id && covered(o, a) && covered(o, b));
  if (!own && b - a >= 60 && other != null) own = { id, a, b, master: other };
}
if (own) {
  console.log(`camera ${own.id} records nothing ${new Date(own.a * 1000).toISOString()} +${own.b - own.a}s; master ${own.master}`);
  await ev((c, id) => c._setMaster(id), own.master);
  await ev((c, t) => c._seekAll(t, true), own.a - 8);
  for (let i = 0; i < 12; i++) { await sleep(1000); if (i > 6) await st(`in +${i + 1}s`); }
  await ev((c, t) => c._seekAll(t, true), own.b - 8);
  for (let i = 0; i < 12; i++) { await sleep(1000); await st(`out +${i + 1}s`); }
  await ev((c, id) => c._setMaster(id), own.id);
  await ev((c, t) => c._seekAll(t, true), Math.floor((own.a + own.b) / 2));
  for (let i = 0; i < 5; i++) { await sleep(1000); await st(`into +${i + 1}s`); }
} else console.log("no single-camera gap of a minute or more in the last week");
if (shared) {
  console.log(`every camera stops ${new Date(shared[0] * 1000).toISOString()}`);
  await ev((c, t) => c._seekAll(t, true), shared[0] - 8);
  for (let i = 0; i < 14; i++) { await sleep(1000); if (i > 5) await st(`all +${i + 1}s`); }
} else console.log("no gap every camera shares in the last week");
await browser.close(); process.exit(0);
