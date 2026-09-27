// The time-lapse card: first frame, the card fitting the screen, seeks (into
// the next file too), another camera and day. Real H.265 decode in Chrome
// (see run.sh). Exits 1 if any check fails.
import { open, sleep } from "./harness.mjs";
const { browser, page, ev } = await open({ w: 1400, h: 800, path: "/ss-playback/timelapse", tag: "ss-timelapse-card" });
page.on("crash", () => console.log("[page crashed]"));
let failed = 0;
const check = (ok, what) => { console.log(`${ok ? "ok  " : "FAIL"} ${what}`); if (!ok) failed++; };
const state = () => ev((c) => {
  const v = c._video, q = v.getVideoPlaybackQuality?.(), f = c._feed;
  const clock = c._session ? new Date(c._wallAt(v.currentTime) * 1000).toISOString().slice(5, 16) : "-";
  return `${c._camera()?.name} ${c._date} t=${v.currentTime.toFixed(1)} ${clock}Z ${v.paused ? "paused" : "playing"} ` +
    `${v.videoWidth}x${v.videoHeight} frames ${q?.totalVideoFrames}/${q?.droppedVideoFrames} dropped, ` +
    `ahead ${f ? ((f.bufferedEnd(v.currentTime) ?? v.currentTime) - v.currentTime).toFixed(1) : "-"} veil="${c._veil.classList.contains("off") ? "" : c._veil.textContent.trim().replace(/\s+/g, " ")}"`;
});
const until = async (fn, ms = 20000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (await ev(fn)) return Date.now() - t0; await sleep(50); } return -1; };
const playingAt = async (label) => {
  // Waits for the playhead to move on from where it is now.
  const t0 = await ev((c) => c._video.currentTime);
  const ms = await until((c) => c._video.currentTime > c._t0 + 0.3 && !c._video.paused, 20000, await ev((c, t) => (c._t0 = t), t0));
  console.log(`${label}: ${ms} ms:`, await state());
  return ms;
};

check(await ev((c) => c._video.poster.startsWith("data:image/gif")), "blank poster (no WebView play button)");
const first = await until((c) => c._video.currentTime > 0.2);
check(first > 0, `first frame after ${first} ms`);
await ev((c) => { c._stalls = 0; c._video.addEventListener("waiting", () => c._stalls++); });

// The whole card on the screen: nothing to scroll to reach the controls.
const fit = await ev((c) => { const r = c.shadowRoot.querySelector(".controls").getBoundingClientRect(); return [Math.round(r.bottom), innerHeight]; });
check(fit[0] <= fit[1], `controls end at ${fit[0]} px of a ${fit[1]} px window`);

// A finished day: the previous one. Seek to noon, then into the second file (after 20:00).
await ev((c) => c._stepDay(1));
check((await until((c) => c._video.currentTime > 0.2)) > 0, "previous day plays");
for (const [h, label] of [[12, "seek to 12:00"], [20, "seek to 20:00 (next file)"], [3, "seek back to 03:00"]]) {
  await ev((c, h) => { c._video.currentTime = c._mediaAt(c._day.start + h * 3600); }, h);
  check((await playingAt(label)) > 0, label);
  const hour = await ev((c) => Number(new Intl.DateTimeFormat("en-US", { timeZone: c._index.timezone, hour: "numeric", hourCycle: "h23" }).format(new Date(c._wallAt(c._video.currentTime) * 1000))));
  check(hour === h, `clock shows ${hour}:xx after seeking to ${h}:00`);
}
await sleep(8000);
console.log("8 s later:", await state());
// Back into a stretch still buffered: carry on from its end, not refetch what's behind.
await ev((c) => { c._before = new Set(c._feed.appended); c._video.currentTime = c._mediaAt(c._day.start + 20 * 3600) + 2; });
await sleep(4000);
const behind = await ev((c) => [...c._feed.appended].filter((k) => !c._before.has(k) && c._feed.starts[k] < c._video.currentTime - 60).length);
check(behind === 0, `nothing appended far behind the playhead after a seek into the buffer (${behind})`);
await page.screenshot({ path: "tl-day.png" });

// Another camera, same date.
await ev((c) => c._selectCamera(c._index.cameras.find((x) => x.id !== c._cameraId).id));
check((await until((c) => c._video.currentTime > 0.2)) > 0, "other camera plays");
console.log("stalls:", await ev((c) => c._stalls), "|", await state());
await browser.close();
process.exit(failed ? 1 : 0);
