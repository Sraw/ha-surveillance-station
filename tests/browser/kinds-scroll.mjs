// On a phone: a short event list (a kind chosen) lets a swipe over it scroll
// the page; a long one scrolls itself and keeps the page still. The short
// list is the kind with the fewest events (it depends on the day's events;
// skipped if even that one overflows).
import { open, sleep } from "./harness.mjs";
const fail = [];
const phone = { w: 412, h: 915, mobile: true };
const probe = await open({ ...phone, prefs: { cameras: [6, 7, 10, 11], grid: true, kinds: [] } });
await sleep(6000);
const fewest = await probe.ev((c) => [...c._evKinds].sort((a, b) => a[1] - b[1])[0]?.[0]);
await probe.browser.close();
for (const [kinds, want] of [[[fewest], "page"], [[], "list"]]) {
  const { browser, page, ev } = await open({ ...phone, prefs: { cameras: [6, 7, 10, 11], grid: true, kinds } });
  await sleep(6000);
  if (want === "page" && (await ev((c) => c._evList.scrollHeight > c._evList.clientHeight + 1))) {
    console.log("skip", fewest, "overflows the list today: no short list to try");
    await browser.close();
    continue;
  }
  const at = await ev((c) => { const x = c._evList.getBoundingClientRect(); return [x.left + x.width / 2, x.top + 100]; });
  await page.mouse.move(...at);
  await page.mouse.wheel(0, 300);
  await sleep(1000);
  const moved = { page: await page.evaluate(() => document.scrollingElement.scrollTop), list: await ev((c) => c._evList.scrollTop) };
  const ok = want === "page" ? moved.page > 0 && moved.list === 0 : moved.list > 0 && moved.page === 0;
  console.log(ok ? "ok  " : "FAIL", JSON.stringify(kinds), "should scroll the", want, JSON.stringify(moved));
  if (!ok) fail.push(want);
  await browser.close();
}
console.log(fail.length ? "FAILED" : "PASS");
process.exit(fail.length ? 1 : 0);
