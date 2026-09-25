// Grid / one camera: in the grid the chips add and remove cameras, with one
// camera they switch it; the grid's cameras and the mode survive a reload.
import { open, sleep } from "./harness.mjs";
const { browser, page, card, ev } = await open({ prefs: { grid: true } });
let failed = false;
await sleep(6000);
const st = () => ev((c) => `grid=${c._grid} shown=[${c._shown}] master=${c._cameraId} gridSet=[${c._gridSet}] ` +
  [...c._players.values()].map((p) => `${p.cameraId}${p.video.paused && !p.loading ? "P" : ">"}`).join(" ") + ` btn="${c.shadowRoot.querySelector('.controls [data-act="solo"]').title}"`);
const step = async (label, fn, want) => {
  await fn(); await sleep(2500);
  const [shown, master, playing] = await ev((c) => [c._shown.join(), c._cameraId, !c._master.video.paused]);
  const ok = shown === want[0] && master === want[1] && playing;
  if (!ok) failed = true;
  console.log(ok ? "ok  " : "FAIL", label.padEnd(26), await st());
};
console.log("start".padEnd(26), await st());
// Camera ids of this installation: 6, 7, 10, 11 (Drive Way first).
const mode = () => card.locator('.controls [data-act="solo"]').click();
await step("mode button -> one", mode, ["6", 6]);
await step("chip Front Door", () => card.locator('[data-cam="10"]').click(), ["10", 10]);
await step("chip Front Door again", () => card.locator('[data-cam="10"]').click(), ["10", 10]);
await step("mode button -> grid", mode, ["6,7,10,11", 10]);
await step("chip BackyardPath (remove)", () => card.locator('[data-cam="7"]').click(), ["6,10,11", 10]);
await step("mode button -> one", mode, ["10", 10]);
await page.reload(); await sleep(6000);
console.log("after reload".padEnd(26), await st());
await step("mode button -> grid", mode, ["6,10,11", 10]);
await step("chip BackyardPath (add)", () => card.locator('[data-cam="7"]').click(), ["6,7,10,11", 10]);
await browser.close(); process.exit(failed ? 1 : 0);
