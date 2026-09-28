// The whole grid: after a jump, skips, a speed change, pause, and a master
// change, how far each camera is from the master. Exits 1 if a check fails.
import { open, snap, sleep, checks } from "./harness.mjs";
const { browser, page, ev } = await open({ prefs: {} });
const { check, done } = checks();
await sleep(8000);
console.log("live grid:", await snap(ev, Date.now() / 1000));
// In step to ~0.15 s once settled (docs/timeline-card.md); a camera in a gap
// of its own holds under its veil instead, and isn't counted.
const TOL = 0.5;
const drift = async (label, tol = null) => {
  const r = await ev((c) => { const m = c._master.wall(); return [...c._players.values()].map((p) => ({ id: p.cameraId, d: p.wall() - m, paused: p.video.paused, gap: !!p.gap, loading: p.loading, master: p === c._master })); });
  console.log(label, r.map((p) => `${p.id}:${p.d.toFixed(2)}${p.paused ? "P" : ""}${p.gap ? "G" : ""}${p.loading ? "L" : ""}`).join(" "));
  if (tol != null) check(r.every((p) => p.master || p.gap || (!p.loading && Math.abs(p.d) < tol)), `${label}: every camera within ${tol} s of the master`);
  return r;
};
const T = Math.floor(Date.now() / 1000) - 3 * 3600 - 600;
await ev((c, T) => c._seekAll(T, true), T);
for (let i = 0; i < 10; i++) { await sleep(1000); await drift(`t+${i + 1}s`, i === 9 ? TOL : null); }
console.log(await snap(ev, await ev((c) => c._master.wall())));
for (const d of [-10, 30]) {
  await ev((c, d) => c.shadowRoot.querySelector(`[data-skip="${d}"]`).click(), d);
  for (let i = 0; i < 4; i++) { await sleep(1000); await drift(`skip ${d} +${i + 1}s`, i === 3 ? TOL : null); }
}
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "4"; s.dispatchEvent(new Event("change")); });
for (let i = 0; i < 5; i++) { await sleep(1500); await drift(`4x +${(i + 1) * 1.5}s`); }
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "1"; s.dispatchEvent(new Event("change")); });
for (let i = 0; i < 3; i++) { await sleep(1500); await drift(`1x +${(i + 1) * 1.5}s`, i === 2 ? TOL : null); }
await ev((c) => c.shadowRoot.querySelector('[data-act="play"]').click());
await sleep(3000);
const paused = await drift("paused 3s");
check(paused.every((p) => p.paused), "paused: every camera paused");
await ev((c) => c.shadowRoot.querySelector('[data-act="play"]').click());
await sleep(2000);
const resumed = await drift("resumed 2s", TOL);
check(resumed.filter((p) => !p.gap).every((p) => !p.paused), "resumed: every camera with footage plays");
// switch master to another camera
const next = await ev((c) => c._shown[2]);
await ev((c, id) => c._setMaster(id), next);
for (let i = 0; i < 3; i++) { await sleep(1000); await drift(`new master +${i + 1}s`, i === 2 ? TOL : null); }
check((await ev((c) => c._master.cameraId)) === next, "the camera tapped is the master");
console.log(await snap(ev, await ev((c) => c._master.wall())));
await page.screenshot({ path: "pb3.png" });
await done(browser);
