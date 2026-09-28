// Sound kept on the video, live and recorded; playback near the present; the
// Live button; a speed chosen while live used by the next recording. Exits 1
// if a check fails.
import { open, snap, sleep, checks } from "./harness.mjs";
const { browser, page, ev } = await open({ prefs: { cameras: [6] } });
const { check, done } = checks();
await sleep(5000);
const sound = () => ev((c) => { const f = c._master.feed, a = f?.audio; if (!a) return null;
  return { started: a.started, paused: a.el.paused, t: a.el.currentTime, rate: a.el.playbackRate, diff: a.started ? a.track.wall(a.el.currentTime) - f.wall(c._master.video.currentTime) : null, err: a.el.error?.code ?? "" }; });
const aud = async () => { const a = await sound(); return a ? `audio started=${a.started} paused=${a.paused} t=${a.t.toFixed(2)} rate=${a.rate} diff=${a.diff?.toFixed(3) ?? "-"} err=${a.err}` : "no audio"; };
// Sound within ~0.1 s of the video (docs/development.md), once it has had a few seconds.
const inStep = async (label) => { const a = await sound(); check(a?.started && !a.paused && Math.abs(a.diff) < 0.15, `${label}: sound playing, ${a?.diff?.toFixed(3) ?? "-"} s off the video`); };
await ev((c) => c.shadowRoot.querySelector('[data-act="mute"]').click());
for (let i = 0; i < 3; i++) { await sleep(1500); console.log("live unmuted:", await aud()); }
await inStep("live");
const T = Math.floor(Date.now() / 1000) - 3600;
await ev((c, T) => c._seekAll(T, true), T);
for (let i = 0; i < 4; i++) { await sleep(1500); console.log("playback unmuted:", await aud()); }
await inStep("recordings");
await ev((c) => c.shadowRoot.querySelector('[data-skip="-10"]').click());
for (let i = 0; i < 2; i++) { await sleep(1500); console.log("after -10:", await aud()); }
await inStep("after -10");
await ev((c) => c.shadowRoot.querySelector('[data-skip="30"]').click());
for (let i = 0; i < 2; i++) { await sleep(1500); console.log("after +30:", await aud()); }
await inStep("after +30");
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "2"; s.dispatchEvent(new Event("change")); });
await sleep(1500); console.log("2x:", await aud());
check((await sound()) === null, "2x: no sound (recordings have it at 1x only)");
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "1"; s.dispatchEvent(new Event("change")); });
await sleep(2500); console.log("1x again:", await aud());
// catch up with now
const N = Math.floor(Date.now() / 1000) - 25;
await ev((c, T) => c._seekAll(T, true), N);
for (let i = 0; i < 8; i++) { await sleep(5000); console.log(`near now +${(i + 1) * 5}s: behind now`, await ev((c) => (Date.now() / 1000 - c._master.wall()).toFixed(1)), "liveTag", await ev((c) => !c._liveTag.hidden), await snap(ev, Date.now() / 1000)); }
const near = await ev((c) => ({ behind: Date.now() / 1000 - c._master.wall(), tag: !c._liveTag.hidden }));
check(near.tag && near.behind < 10, `recordings played into the present are live (${near.behind.toFixed(1)} s behind, LIVE shown)`);
await ev((c) => c.shadowRoot.querySelector('[data-act="live"]').click());
await sleep(3000); console.log("Live button:", await snap(ev, Date.now() / 1000), await aud());
check(await ev((c) => !!c._master.feed?.live && !c._master.video.paused), "Live button: the real-time stream, playing");
await ev((c) => { const s = c.shadowRoot.querySelector(".speed"); s.value = "4"; s.dispatchEvent(new Event("change")); });
await sleep(2000); console.log("4x while live:", await snap(ev, Date.now() / 1000));
check(await ev((c) => c._master.feed?.live && c._master.feed.speed === 1), "4x while live: live stays at 1x");
const U = Math.floor(Date.now() / 1000) - 7200;
await ev((c, T) => c._seekAll(T, true), U);
const w0 = await ev((c) => c._master.wall()); await sleep(4000);
const rate = ((await ev((c) => c._master.wall())) - w0) / 4;
console.log("history at 4x: footage/s", rate.toFixed(2), await aud());
check(rate > 2.5 && rate < 5, `the speed chosen while live plays the next recording (${rate.toFixed(2)} s of footage a second)`);
check((await sound()) === null, "4x: no sound");
await done(browser);
