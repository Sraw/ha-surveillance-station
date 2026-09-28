// Kind chips narrow the list and the pins; Frigate's smart search (words,
// "similar to" a bookmark) lists results that play from SS.
import { open, sleep } from "./harness.mjs";
const { browser, page, ev } = await open({ prefs: { cameras: [6, 7, 10, 11], grid: true } });
const fail = [];
const check = (ok, what) => { console.log(ok ? "ok  " : "FAIL", what); if (!ok) fail.push(what); };
await sleep(6000);
const rows = () => ev((c) => [...c.shadowRoot.querySelectorAll(".ev-items .ev")].map((b) => b.querySelector(".n").textContent));
const state = () => ev((c) => ({
  search: !c.shadowRoot.querySelector(".ev-search").hidden,
  chips: [...c.shadowRoot.querySelectorAll(".ev-kinds button")].map((b) => b.textContent.trim()),
  pins: c.shadowRoot.querySelectorAll(".pins .bm").length,
  foot: c.shadowRoot.querySelector(".ev-foot").textContent,
  sims: c.shadowRoot.querySelectorAll(".ev-items .sim").length,
}));
let s = await state();
console.log("start:", JSON.stringify(s), (await rows()).slice(0, 5));
check(s.search, "search box shown (Frigate URL set)");
check(s.chips.length >= 2, "kind chips shown");
check(s.sims > 0, "similar buttons on Frigate bookmarks");
const pins0 = s.pins;

// Only cars.
await ev((c) => [...c.shadowRoot.querySelectorAll(".ev-kinds button")].find((b) => b.dataset.kind === "Car").click());
await sleep(2500);
const cars = await rows();
s = await state();
console.log("Car only:", cars.slice(0, 6), "pins", pins0, "->", s.pins);
check(cars.length > 0 && cars.every((n) => n.split(", ").includes("Car")), "list is only bookmarks with Car");
check(s.pins <= pins0, "pins narrowed");
await ev((c) => c.shadowRoot.querySelector('.ev-kinds button[data-kind="Car"]').click());
await sleep(2000);

// Words.
await ev((c) => { const i = c.shadowRoot.querySelector(".ev-search input"); i.value = "white car"; i.form.requestSubmit(); });
for (let i = 0; i < 20 && (await ev((c) => c._search?.loading)); i++) await sleep(500);
const found = await ev((c) => c._search.items.map((r) => `${r.name}|${c._cameraName(r.camera_id)}|${new Date(r.start * 1000).toLocaleString()}|bm=${r.bookmark_id}`));
console.log("white car:", found.slice(0, 6), "error:", await ev((c) => c._search.error));
check(found.length > 0, "search found results");
await sleep(3000);
for (let i = 0; i < 20 && !(await ev((c) => [...c.shadowRoot.querySelectorAll(".ev-items img")].slice(0, 5).every((img) => img.naturalWidth))); i++) await sleep(1000);
const imgs = await ev((c) => [...c.shadowRoot.querySelectorAll(".ev-items img")].slice(0, 5).map((i) => [i.naturalWidth, i.naturalHeight]));
console.log("thumbnail sizes:", JSON.stringify(imgs));
check(imgs.length > 0 && imgs.every(([w]) => w > 0), "result thumbnails load");
// These cameras' main streams are all 16:9 (not a square crop).
check(imgs.every(([w, h]) => Math.abs(w / h - 16 / 9) < 0.05), "result thumbnails are 16:9 (the bookmark's SS frame)");
await page.screenshot({ path: "search-results.png" });

// A chip chosen during a search: after closing it, the list is of that kind.
await ev((c) => [...c.shadowRoot.querySelectorAll(".ev-kinds button")].find((b) => b.dataset.kind === "Person").click());
await sleep(500);
for (let i = 0; i < 20 && (await ev((c) => c._search?.loading)); i++) await sleep(500);
const byKind = await ev((c) => ({ kinds: c._search.kinds, names: c._search.items.map((r) => r.name) }));
console.log("white car, Person chosen:", JSON.stringify(byKind).slice(0, 200));
check(byKind.kinds === "person" && byKind.names.every((n) => n.split(", ").includes("Person")), "a chip chosen during a search asks it again, of that kind");
await ev((c) => c.shadowRoot.querySelector('[data-act="search-close"]').click());
await sleep(1500);
const persons = await rows();
check(persons.length > 0 && persons.every((n) => n.split(", ").includes("Person")), "after the search, the list is of the chip chosen meanwhile");
await ev((c) => c.shadowRoot.querySelector('.ev-kinds button[data-kind="Person"]').click());
await sleep(2000);
await ev((c) => { const i = c.shadowRoot.querySelector(".ev-search input"); i.value = "white car"; i.form.requestSubmit(); });
for (let i = 0; i < 20 && (await ev((c) => c._search?.loading)); i++) await sleep(500);

// Tap the first: plays that camera from 3 s before.
const first = await ev((c) => c._search.items[0]);
await ev((c) => c.shadowRoot.querySelector(".ev-items .ev").click());
await sleep(5000);
const at = await ev((c) => ({ cam: c._master.cameraId, wall: c._master.wall() }));
console.log("played:", at.cam, "at", (at.wall - first.start).toFixed(1), "s from the result's start");
check(at.cam === first.camera_id && Math.abs(at.wall - first.start) < 8, "result plays its camera near its time");
const lit = await ev((c) => [...c.shadowRoot.querySelectorAll(".ev-items .ev.on")].map((b) => b.dataset.sr));
check(lit.includes(first.key), "the result playing is highlighted");

// Back, then "similar" on a bookmark.
await ev((c) => c.shadowRoot.querySelector('[data-act="search-close"]').click());
await sleep(1500);
check(!(await ev((c) => c._search)), "closing the search goes back to the bookmarks");
await ev((c) => c.shadowRoot.querySelector(".ev-items .sim").click());
for (let i = 0; i < 20 && (await ev((c) => c._search?.loading)); i++) await sleep(500);
const sim = await ev((c) => ({ label: c.shadowRoot.querySelector(".ev-sq .t").textContent, n: c._search.items.length, err: c._search.error }));
console.log("similar:", JSON.stringify(sim));
check(sim.n > 0 && !sim.err, "similar to a bookmark finds results");
await page.screenshot({ path: "search-similar.png" });
console.log(fail.length ? `FAILED: ${fail.join("; ")}` : "PASS");
await browser.close(); process.exit(fail.length ? 1 : 0);
