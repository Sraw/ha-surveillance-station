// The whole grid: after a jump, skips, a speed change, pause, and a master
// change, how far each camera is from the master.
import { open, snap, sleep } from "./harness.mjs";
const { browser, page, ev } = await open({ prefs: {} });
await sleep(8000);
console.log("live grid:", await snap(ev, Date.now() / 1000));
const drift = async (label) => {
  const r = await ev((c) => { const m = c._master.wall(); return [...c._players.values()].map((p) => `${p.cameraId}:${(p.wall() - m).toFixed(2)}${p.video.paused ? "P" : ""}${p.gap ? "G" : ""}${p.loading ? "L" : ""}`).join(" "); });
  console.log(label, r);
};
const T = Math.floor(Date.now() / 1000) - 3 * 3600 - 600;
await ev((c, T) => c._seekAll(T, true), T);
for (let i = 0; i < 10; i++) { await sleep(1000); await drift(`t+${i + 1}s`); }
console.log(await snap(ev, await ev((c) => c._master.wall())));
for (const d of [-10, 30]) {
  await ev((c, d) => c.shadowRoot.querySelector(`[data-skip="${d}"]`).click(), d);
  for (let i = 0; i < 4; i++) { await sleep(1000); await drift(`skip ${d} +${i + 1}s`); }
}
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "4"; s.dispatchEvent(new Event("change")); });
for (let i = 0; i < 5; i++) { await sleep(1500); await drift(`4x +${(i + 1) * 1.5}s`); }
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "1"; s.dispatchEvent(new Event("change")); });
for (let i = 0; i < 3; i++) { await sleep(1500); await drift(`1x +${(i + 1) * 1.5}s`); }
await ev((c) => c.shadowRoot.querySelector('[data-act="play"]').click());
await sleep(3000); await drift("paused 3s");
await ev((c) => c.shadowRoot.querySelector('[data-act="play"]').click());
await sleep(2000); await drift("resumed 2s");
// switch master to another camera
await ev((c) => c._setMaster(c._shown[2]));
for (let i = 0; i < 3; i++) { await sleep(1000); await drift(`new master +${i + 1}s`); }
console.log(await snap(ev, await ev((c) => c._master.wall())));
await page.screenshot({ path: "pb3.png" });
await browser.close(); process.exit(0);
