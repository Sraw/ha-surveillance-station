// The mute card: the hierarchy of the mute switches, driven like a user would
// (cameras expand, all-kinds and kinds follow each other, everything mutes the
// rest). Puts every mute back at the end. Needs a view holding the card:
//   DASHBOARD=/ss-playback/notifications tests/browser/run.sh mute-card.mjs
import { open, sleep } from "./harness.mjs";
const { browser, page, ev } = await open({ w: 900, h: 1100, path: process.env.DASHBOARD ?? "/ss-playback/notifications", tag: "ss-mute-card" });
let failed = 0;
const check = (ok, what) => { console.log(`${ok ? "ok  " : "FAIL"} ${what}`); if (!ok) failed++; };
// The card's rows: name -> {on, gone, sub}, in order (kind rows are indented ones).
const rows = () => ev((c) => [...c.shadowRoot.querySelectorAll(".row")].map((r) => ({
  name: r.querySelector(".name").firstChild.textContent.trim(), sub: r.querySelector("small")?.textContent ?? "",
  kind: r.classList.contains("kind"), on: r.classList.contains("on"), gone: r.classList.contains("unavailable"),
  sw: r.querySelector("ha-switch").dataset.e })));
const settle = () => sleep(1200);
const click = async (sel) => { await page.locator(`ss-mute-card ${sel}`).first().click(); await settle(); };
const row = async (name, kind = false) => (await rows()).find((r) => r.name === name && r.kind === kind);
const toggle = async (name) => click(`ha-switch[data-e="${(await row(name)).sw}"]`);
const toggleSw = (r) => click(`ha-switch[data-e="${r.sw}"]`);

await page.locator("ss-mute-card .row").first().waitFor({ timeout: 15000 });
await page.screenshot({ path: "mute-card-1.png" });
let all = await rows();
console.log(all.map((r) => `${r.kind ? "  " : ""}${r.name}${r.on ? " [on]" : ""}${r.gone ? " [unavailable]" : ""}${r.sub ? " (" + r.sub + ")" : ""}`).join("\n"));
check(all.some((r) => r.name === "Everything"), "Everything row");
check(["Person", "Car", "Animal"].every((k) => all.some((r) => r.name === k && r.kind)), "the three kinds for every camera");
const cameras = all.filter((r) => !r.kind && r.name !== "Everything").map((r) => r.name);
check(cameras.length >= 2, `cameras found: ${cameras.join(", ")}`);
check(all.every((r) => !r.on), "starts with nothing muted (else this run would put it wrong)");
const cam = cameras[0];

// A camera's kinds are behind its arrow.
check(!(await rows()).some((r) => r.kind && r.sw.includes(cam.toLowerCase().replace(/\W/g, "_"))), `${cam}: kinds hidden until expanded`);
await click(`button.chevron[data-camera="${cam}"]`);
const kindsOf = async () => (await rows()).filter((r) => r.kind && r.sw.includes("_" + cam.toLowerCase().replace(/\W/g, "_") + "_"));
check((await kindsOf()).length === 3, `${cam}: expanded shows three kinds`);

// All kinds on -> its kinds on; one kind off -> all kinds off, the others still on.
await toggle(cam);
check((await row(cam)).on && (await kindsOf()).every((r) => r.on), "all kinds on turns its kinds on");
check((await row(cam)).sub === "forever", `until shows forever (${(await row(cam)).sub})`);
await toggleSw((await kindsOf())[1]);
const after = await kindsOf();
check(!(await row(cam)).on && after.map((r) => r.on).join() === "true,false,true", `one kind off: all kinds off, others still muted (${after.map((r) => r.on)})`);
check((await row(cam)).sub.includes("muted"), `partial status: "${(await row(cam)).sub}"`);
await page.screenshot({ path: "mute-card-2.png" });
await toggleSw((await kindsOf())[1]);
check((await row(cam)).on, "every kind on shows all kinds on");
await toggle(cam);
check(!(await row(cam)).on && (await kindsOf()).length === 3 && (await kindsOf()).every((r) => !r.on), "all kinds off lifts them all");

// A kind for every camera makes that kind's camera rows unavailable.
const person = all.find((r) => r.name === "Person" && r.kind);
await toggleSw(person);
check((await kindsOf())[0].gone && (await kindsOf())[0].on && !(await kindsOf())[1].gone, "kind muted for every camera: that kind on each camera is on and locked");
await toggleSw(person);

// Everything muted: the rest stand aside.
await toggle("Everything");
all = await rows();
check(all.length > 8 && (await rows()).filter((r) => r.name !== "Everything").every((r) => r.gone && r.on), "everything muted: every other switch shown on and locked");
check((await row("Everything")).sub === "forever", "Everything shows forever");
await page.screenshot({ path: "mute-card-3.png" });
await toggle("Everything");
check((await rows()).every((r) => !r.on && !r.gone), "back to nothing muted");

// The buttons.
await click("button[data-mute]");
check((await row("Everything")).on && /\d{4}-\d\d-\d\d \d\d:\d\d/.test((await row("Everything")).sub), `Mute 1 h: ${(await row("Everything")).sub}`);
await click("button[data-unmute]");
check((await rows()).every((r) => !r.on), "Unmute all");
await browser.close();
console.log(failed ? `${failed} FAILED` : "all ok");
process.exit(failed ? 1 : 0);
