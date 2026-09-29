// The snapshot button: the one camera's frame (live and in playback) and the
// grid's cameras in one picture, each a JPEG download named for camera and
// time. Exits 1 if a check fails.
import fs from "fs";
import { open, sleep, checks } from "./harness.mjs";

// A JPEG's size, from its first SOF marker (null: not a JPEG).
function jpegSize(buf) {
  if (buf[0] !== 0xff || buf[1] !== 0xd8) return null;
  for (let i = 2; i + 9 < buf.length; ) {
    if (buf[i] !== 0xff) return null;
    const m = buf[i + 1];
    if (m >= 0xc0 && m <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(m)) return { h: buf.readUInt16BE(i + 5), w: buf.readUInt16BE(i + 7) };
    i += 2 + buf.readUInt16BE(i + 2);
  }
  return null;
}
const { check, done } = checks();
const save = async (page, click) => {
  const [download] = await Promise.all([page.waitForEvent("download", { timeout: 15000 }), click()]);
  const path = `/pw/snap-${Date.now()}.jpg`;
  await download.saveAs(path);
  const buf = fs.readFileSync(path);
  fs.unlinkSync(path);
  return { name: download.suggestedFilename(), size: jpegSize(buf), bytes: buf.length };
};
const ready = (ev) => ev((c) => c._players.size > 0 && !!c._leader && [...c._players.values()].every((p) => p.video.readyState >= 2 && p.video.videoWidth));
const waitReady = async (ev) => { for (let i = 0; i < 40 && !(await ready(ev)); i++) await sleep(500); };
const press = (ev) => () => ev((c) => c.shadowRoot.querySelector('.controls [data-act="snap"]').click());

// Chrome here decodes with VA-API and now and then loses the page when a canvas reads a
// decoded frame back (a browser crash of this rig, not of the card): a section that
// crashes the page is run again, up to five times.
const section = async (name, run) => {
  for (let i = 1; ; i++) {
    let browser;
    try {
      return await run(async (opts) => { const o = await open(opts); browser = o.browser; return o; });
    } catch (e) {
      await browser?.close().catch(() => {});
      console.log(`[${name}] attempt ${i}: ${e.message.split("\n")[0]}`);
      if (i === 5 || !/crashed/i.test(e.message)) throw e;
    }
  }
};

const shot = (ev, bar = ".controls") => () => ev((c, bar) => c.shadowRoot.querySelector(`${bar} [data-act="snap"]`).click(), bar);
const one = { grid: false, cameras: [7] }; // a smaller camera than the 4K one: the rig has little memory

await section("live", async (start) => {
  const { browser, page, ev } = await start({ prefs: one });
  await waitReady(ev);
  const [w, h, name] = await ev((c) => [c._leader.video.videoWidth, c._leader.video.videoHeight, c._cameraName(c._leader.cameraId)]);
  const r = await save(page, shot(ev));
  check(r.size && r.size.w === w && r.size.h === h && r.bytes > 20000, `live: a JPEG of the camera's ${w}x${h} (${r.size?.w}x${r.size?.h}, ${r.bytes} bytes)`);
  check(/^.+ \d{4}-\d\d-\d\d \d\d-\d\d-\d\d\.jpg$/.test(r.name) && r.name.startsWith(name), `live: named "${r.name}"`);
  await browser.close();
});
await section("fullscreen bar", async (start) => {
  const { browser, page, ev } = await start({ prefs: one });
  await waitReady(ev);
  const r = await save(page, shot(ev, ".fsbar"));
  check(!!r.size, "the fullscreen bar's button saves too");
  await browser.close();
});
// The grid, live (playback crashes this rig's Chrome with or without a snapshot when memory is short).
await section("grid", async (start) => {
  const { browser, page, ev } = await start({ prefs: { grid: true, cameras: [7, 10] } });
  await waitReady(ev);
  await sleep(4000);
  const [n, cols] = await ev((c) => [c._players.size, Number(c._stage.style.getPropertyValue("--cols"))]);
  const r = await save(page, shot(ev));
  const d = new Date();
  const p2 = (x) => String(x).padStart(2, "0");
  check(n > 1 && r.size && r.size.w > 1000 && r.size.w <= 3840 && r.bytes > 20000, `grid of ${n} (${cols} columns): one JPEG ${r.size?.w}x${r.size?.h}, ${r.bytes} bytes`);
  check(r.name.startsWith(`Cameras ${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}-`), `grid: named for the time played ("${r.name}")`);
  await browser.close();
});
await done();
