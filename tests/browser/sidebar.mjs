// The event list: beside the video on a wide screen it reaches the bottom of
// the screen (it was capped at 60vh, half the screen on a tall one); under
// it on a phone it keeps its 60vh cap. Exits 1 if a check fails.
import { open, sleep } from "./harness.mjs";
let failed = 0;
for (const [w, h, mobile] of [[1920, 1080], [1920, 1400], [1300, 1200], [390, 844, true]]) {
  const { browser, ev } = await open({ w, h, mobile });
  await sleep(6000);
  const m = await ev((c) => {
    const box = (sel) => c.shadowRoot.querySelector(sel).getBoundingClientRect();
    return { list: Math.round(box(".ev-list").bottom), listH: Math.round(box(".ev-list").height), stage: Math.round(box(".stage").height), inner: innerHeight };
  });
  const ok = mobile ? m.listH <= m.inner * 0.6 + 10 : m.list >= m.inner - 20; // 60vh + its 8 px padding
  console.log(`${ok ? "ok  " : "FAIL"} ${w}x${h}: list ends at ${m.list} (height ${m.listH}) of ${m.inner}, stage ${m.stage} px`);
  if (!ok) failed++;
  await browser.close();
}
process.exit(failed ? 1 : 0);
