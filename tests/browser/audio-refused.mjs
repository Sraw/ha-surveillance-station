// Audio the browser says it plays but refuses all the same (addSourceBuffer
// throws): the video goes on, the mute button says the sound can't play here.
import { open, sleep } from "./harness.mjs";
const { browser, page, ev } = await open({ prefs: { cameras: [6] } });
await page.evaluate(() => {
  for (const MS of [window.MediaSource, window.ManagedMediaSource].filter(Boolean)) {
    const add = MS.prototype.addSourceBuffer;
    MS.prototype.addSourceBuffer = function (mime) {
      if (mime.startsWith("audio/")) throw new DOMException("refused (test)", "NotSupportedError");
      return add.call(this, mime);
    };
  }
});
await sleep(5000);
await ev((c) => c.shadowRoot.querySelector('[data-act="mute"]').click());
await sleep(3000);
const state = () => ev((c) => {
  const p = c._leader, b = c.shadowRoot.querySelector('[data-act="mute"]');
  return { unplayable: p.audioUnplayable ?? null, audio: !!p.feed?.audio, icon: b.querySelector("ha-icon")?.getAttribute("icon"), title: b.title,
    playing: !p.video.paused, t: p.video.currentTime.toFixed(1) };
});
const s = await state();
console.log("after unmute:", JSON.stringify(s));
await sleep(2000);
const s2 = await state();
console.log("2 s later:", JSON.stringify(s2));
const ok = s.unplayable && !s.audio && /volume-variant-off/.test(s.icon ?? "") && s2.playing && +s2.t > +s.t;
console.log(ok ? "PASS" : "FAIL");
await browser.close(); process.exit(ok ? 0 : 1);
