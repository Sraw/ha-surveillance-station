// Sound kept on the video, live and recorded; playback near the present; the
// Live button; a speed chosen while live used by the next recording.
import { open, snap, sleep } from "./harness.mjs";
const { browser, page, ev } = await open({ prefs: { cameras: [6] } });
await sleep(5000);
const aud = () => ev((c) => { const f = c._master.feed, a = f?.audio; if (!a) return "no audio";
  return `audio started=${a.started} paused=${a.el.paused} t=${a.el.currentTime.toFixed(2)} rate=${a.el.playbackRate} diff=${a.started ? (a.track.wall(a.el.currentTime) - f.wall(c._master.video.currentTime)).toFixed(3) : "-"} err=${a.el.error?.code ?? ""}`; });
await ev((c) => c.shadowRoot.querySelector('[data-act="mute"]').click());
for (let i = 0; i < 3; i++) { await sleep(1500); console.log("live unmuted:", await aud()); }
const T = Math.floor(Date.now() / 1000) - 3600;
await ev((c, T) => c._seekAll(T, true), T);
for (let i = 0; i < 4; i++) { await sleep(1500); console.log("playback unmuted:", await aud()); }
await ev((c) => c.shadowRoot.querySelector('[data-skip="-10"]').click());
for (let i = 0; i < 2; i++) { await sleep(1500); console.log("after -10:", await aud()); }
await ev((c) => c.shadowRoot.querySelector('[data-skip="30"]').click());
for (let i = 0; i < 2; i++) { await sleep(1500); console.log("after +30:", await aud()); }
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "2"; s.dispatchEvent(new Event("change")); });
await sleep(1500); console.log("2x:", await aud());
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "1"; s.dispatchEvent(new Event("change")); });
await sleep(2500); console.log("1x again:", await aud());
// catch up with now
const N = Math.floor(Date.now() / 1000) - 25;
await ev((c, T) => c._seekAll(T, true), N);
for (let i = 0; i < 8; i++) { await sleep(5000); console.log(`near now +${(i + 1) * 5}s: behind now`, await ev((c) => (Date.now() / 1000 - c._master.wall()).toFixed(1)), "liveTag", await ev((c) => !c._liveTag.hidden), await snap(ev, Date.now() / 1000)); }
await ev((c) => c.shadowRoot.querySelector('[data-act="live"]').click());
await sleep(3000); console.log("Live button:", await snap(ev, Date.now() / 1000), await aud());
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "4"; s.dispatchEvent(new Event("change")); });
await sleep(2000); console.log("4x while live:", await snap(ev, Date.now() / 1000));
const U = Math.floor(Date.now() / 1000) - 7200;
await ev((c, T) => c._seekAll(T, true), U);
const w0 = await ev((c) => c._master.wall()); await sleep(4000);
console.log("history at 4x: footage/s", (((await ev((c) => c._master.wall())) - w0) / 4).toFixed(2), await aud());
await browser.close(); process.exit(0);
