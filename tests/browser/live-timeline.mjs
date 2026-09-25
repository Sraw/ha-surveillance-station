// Live: the timeline scrolls with now (its right edge stays span x 5 % ahead),
// the playhead stays put near the right; a pan back pauses that for a while.
import { open, sleep } from "./harness.mjs";
const { browser, card, ev } = await open({ prefs: { cameras: [6], grid: false, span: 900 } });
await sleep(5000);
let failed = false;
const at = () => ev((c) => ({ ahead: c._view.end - Date.now() / 1000, span: c._view.end - c._view.start, ph: parseFloat(c._ph.style.left) }));
for (let i = 0; i < 4; i++) {
  const v = await at();
  const ok = Math.abs(v.ahead - v.span * 0.05) < 3 && v.ph > 90 && v.ph < 96;
  if (!ok) failed = true;
  console.log(ok ? "ok  " : "FAIL", `view end now+${v.ahead.toFixed(1)}s (span ${v.span}), playhead at ${v.ph.toFixed(2)}%`);
  await sleep(5000);
}
// A recording in progress is looked at again every ~5 s. Its end as SS moves
// it (up to ~10 s behind) is still drawn to now; one HA says has stopped (SS
// no longer moves its end) stops at that end instead of growing.
const recEnd = () => ev((c) => {
  const w = c._track.getBoundingClientRect().width;
  const ends = [...c._bars.querySelectorAll(".rec")].map((r) => (r.offsetLeft + r.offsetWidth) / w);
  return { end: Math.max(...ends) * c._span + c._view.start - Date.now() / 1000, calls: c._recCalls };
});
await ev((c) => {
  c._recCalls = 0;
  c._realWs = c._ws;
  c._ws = async (m) => {
    const r = await c._realWs(m);
    if (m.type !== "surveillance_station/recordings") return r;
    c._recCalls++;
    if (c._frozen == null) return r;
    const at = Date.now() / 1000 - c._frozen;
    return { ...r, recordings: r.recordings.map((x) => (x.live ? { ...x, end: at, live: c._frozen <= 15 } : x)) };
  };
});
await sleep(12000);
let r = await recEnd();
let ok2 = r.calls >= 2 && Math.abs(r.end) < 3;
if (!ok2) failed = true;
console.log(ok2 ? "ok  " : "FAIL", `recordings fetched ${r.calls}x in 12 s; bar ends now${r.end.toFixed(1)}s`);
await ev((c) => (c._frozen = 10));
await sleep(7000);
r = await recEnd();
ok2 = Math.abs(r.end) < 3;
if (!ok2) failed = true;
console.log(ok2 ? "ok  " : "FAIL", `SS end 10 s behind, still recording: bar ends now${r.end.toFixed(1)}s`);
await ev((c) => (c._frozen = 40));
await sleep(7000);
r = await recEnd();
ok2 = r.end < -38 && r.end > -42;
if (!ok2) failed = true;
console.log(ok2 ? "ok  " : "FAIL", `stopped 40 s ago: bar ends now${r.end.toFixed(1)}s`);
await ev((c) => { c._ws = c._realWs; });
await sleep(8000);
r = await recEnd();
ok2 = Math.abs(r.end) < 3;
if (!ok2) failed = true;
console.log(ok2 ? "ok  " : "FAIL", `back to real data: bar ends now${r.end.toFixed(1)}s`);

await card.locator('[data-act="pan-back"]').click();
await sleep(3000);
const p = await at();
const ok = p.ahead < -300;
if (!ok) failed = true;
console.log(ok ? "ok  " : "FAIL", `after a pan back the view stays there: end now${p.ahead.toFixed(0)}s`);
// Once the pause is over it comes back to now, with the recordings there fetched.
await sleep(15000);
const b = await ev((c) => {
  const w = c._track.getBoundingClientRect().width;
  const covered = [...c._bars.querySelectorAll(".rec")].some((r) => (r.offsetLeft + r.offsetWidth) / w > 0.9);
  return { ahead: c._view.end - Date.now() / 1000, span: c._view.end - c._view.start, covered };
});
const back = Math.abs(b.ahead - b.span * 0.05) < 3 && b.covered;
if (!back) failed = true;
console.log(back ? "ok  " : "FAIL", `back at now+${b.ahead.toFixed(1)}s, recording bar reaches now: ${b.covered}`);
// The pointer resting on the track holds the view still (tooltips, hover time).
const box = await card.locator(".track").boundingBox();
await card.page().mouse.move(box.x + box.width / 2, box.y + box.height / 2);
const h0 = await at();
await sleep(4000);
const h1 = await at();
const held = Math.abs(h1.ahead - h0.ahead - -4) < 1;
await card.page().mouse.move(0, 0);
await sleep(2000);
const h2 = await at();
const resumed = Math.abs(h2.ahead - h2.span * 0.05) < 3;
if (!held || !resumed) failed = true;
console.log(held && resumed ? "ok  " : "FAIL", `under the pointer the view holds (${h0.ahead.toFixed(1)} -> ${h1.ahead.toFixed(1)}), then follows again (${h2.ahead.toFixed(1)})`);
await browser.close(); process.exit(failed ? 1 : 0);
