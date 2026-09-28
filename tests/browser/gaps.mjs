// Recording gaps in a grid: a follower with nothing recorded shows "No
// recording" until the leader reaches its next footage; a leader sent into
// its own gap lands on its next footage and takes the others there; a gap
// every camera shares is skipped over. The gaps are found in the last week.
// Exits 1 if a check fails.
import { open, sleep, checks } from "./harness.mjs";
const { browser, ev } = await open({ prefs: {} });
const { check, done } = checks();
await sleep(6000);
// Prints a line, and returns the leader's time and each camera's state.
const st = async (label) => {
  const s = await ev((c) => {
    const m = c._leader.wall();
    const players = [...c._players.values()].map((p) => ({ id: p.cameraId, leader: c._leader === p, d: p.wall() - m, paused: p.video.paused, gap: !!p.gap, loading: p.loading,
      veil: p.veilKind ? p.veil.textContent.trim().replace(/\s+/g, " ").slice(0, 20) : "" }));
    return { m, players };
  });
  console.log(label.padEnd(10), new Date(s.m * 1000).toISOString().slice(11, 19) + " | " + s.players.map((p) => `${p.leader ? "*" : ""}${p.id}:${p.d.toFixed(1)}${p.paused ? "P" : ""}${p.gap ? "G" : ""}${p.loading ? "L" : ""}${p.veil ? "[" + p.veil + "]" : ""}`).join(" "));
  return s;
};
// Followers with footage then are within 0.5 s of the leader (a gap of their own: under the veil).
const inStep = (p) => !p.gap && !p.loading && Math.abs(p.d) < 0.5;
const othersInStep = (s) => s.players.every((p) => p.leader || p.gap || inStep(p));
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
  if (!own && b - a >= 60 && other != null) own = { id, a, b, leader: other };
}
if (own) {
  console.log(`camera ${own.id} records nothing ${new Date(own.a * 1000).toISOString()} +${own.b - own.a}s; leader ${own.leader}`);
  await ev((c, id) => c._setLeader(id), own.leader);
  await ev((c, t) => c._seekAll(t, true), own.a - 8);
  let s;
  for (let i = 0; i < 12; i++) { await sleep(1000); if (i > 6) s = await st(`in +${i + 1}s`); }
  const cam = s.players.find((p) => p.id === own.id);
  check(cam.gap && cam.veil.startsWith("No recording"), `camera ${own.id} in its gap: "No recording"`);
  check(s.m > own.a && s.players.find((p) => p.leader).paused === false, "the leader plays on past it");
  await ev((c, t) => c._seekAll(t, true), own.b - 8);
  for (let i = 0; i < 12; i++) { await sleep(1000); s = await st(`out +${i + 1}s`); }
  check(s.m > own.b && inStep(s.players.find((p) => p.id === own.id)) && othersInStep(s), `past the gap's end: camera ${own.id} back in step`);
  await ev((c, id) => c._setLeader(id), own.id);
  await ev((c, t) => c._seekAll(t, true), Math.floor((own.a + own.b) / 2));
  for (let i = 0; i < 5; i++) { await sleep(1000); s = await st(`into +${i + 1}s`); }
  // SS starts at the keyframe before the next footage.
  check(s.m >= own.b - 2 && othersInStep(s), "a leader sent into its gap lands on its next footage, the others with it");
} else console.log("no single-camera gap of a minute or more in the last week");
if (shared) {
  console.log(`every camera stops ${new Date(shared[0] * 1000).toISOString()}`);
  await ev((c, t) => c._seekAll(t, true), shared[0] - 8);
  let s;
  for (let i = 0; i < 14; i++) { await sleep(1000); if (i > 5) s = await st(`all +${i + 1}s`); }
  check(s.m > shared[0] + 5 && !s.players.find((p) => p.leader).paused, "a gap every camera shares is skipped over");
} else console.log("no gap every camera shares in the last week");
await done(browser);
