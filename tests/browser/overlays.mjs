// The "what's shown on the video" menu: time, LIVE badge and camera names
// hide separately, and the choice sticks across a reload.
import { open, sleep } from "./harness.mjs";
const { browser, page, card, ev } = await open({ prefs: { grid: true } });
let failed = false;
const expect = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failed = true;
  console.log(ok ? "ok  " : "FAIL", label, JSON.stringify(got));
};
await sleep(5000);
const vis = () => ev((c) => ({ time: getComputedStyle(c.shadowRoot.querySelector(".clock")).display !== "none",
  live: !c._liveTag.hidden && getComputedStyle(c._liveTag).display !== "none",
  names: [...c.shadowRoot.querySelectorAll(".label")].filter((l) => getComputedStyle(l).display !== "none").length,
  icon: c.shadowRoot.querySelector('[data-act="shows"] ha-icon').getAttribute("icon") }));
const v0 = await vis();
expect("start", [v0.time, v0.names > 1], [true, true]);
await card.locator('[data-act="shows"]').click();
for (const k of ["clock", "live_badge", "camera_names"]) {
  await card.locator(`[data-show="${k}"]`).click();
  const v = await vis();
  expect(`hid ${k}`, [v.time, v.names, v.icon], [false, k === "camera_names" ? 0 : v0.names, "mdi:eye-off-outline"]);
}
await card.locator(`[data-show="live_badge"]`).click();
expect("live badge back", (await vis()).live, true);
await page.mouse.click(700, 300); // elsewhere: the menu closes
expect("menu closed by a tap elsewhere", await ev((c) => c._showsBox.hidden), true);
await page.reload(); await sleep(5000);
const v1 = await vis();
expect("after reload", [v1.time, v1.live, v1.names], [false, true, 0]);
await browser.close(); process.exit(failed ? 1 : 0);
