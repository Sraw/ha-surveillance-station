// Real-HEVC harness: Chrome + VA-API in the hevc-chrome container. The cards
// and ss-common.js are served from the repo (mounted at /card), everything
// else is live HA.
import { chromium } from "playwright";
import fs from "fs";
export const base = process.env.HA_URL; // run.sh requires it
const token = fs.readFileSync("/token", "utf8").trim().split(/\s+/)[0];
// path: a dashboard view holding the card (DASHBOARD, default /ss-playback/playback).
export async function open({ w = 1400, h = 900, mobile = false, path = process.env.DASHBOARD ?? "/ss-playback/playback", prefs = null, tag = "ss-timeline-card" } = {}) {
  const browser = await chromium.launch({ executablePath: "/usr/bin/google-chrome", headless: false,
    args: ["--ozone-platform=wayland", "--no-sandbox", "--autoplay-policy=no-user-gesture-required"] });
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: mobile ? 2 : 1, hasTouch: mobile, isMobile: mobile });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("[pageerror]", e.message));
  page.on("console", (m) => (m.type() === "error" || m.text().startsWith("[t]")) && console.log("[console]", m.text()));
  await page.route(/\/surveillance_station_static\/(ss-[a-z-]+\.js)/, (r) =>
    r.fulfill({ status: 200, contentType: "text/javascript",
      body: fs.readFileSync("/card/" + new URL(r.request().url()).pathname.split("/").pop()) }));
  await page.addInitScript(([t, b, prefs]) => {
    localStorage.setItem("hassTokens", JSON.stringify({ access_token: t, token_type: "Bearer", expires_in: 1e9, hassUrl: b, clientId: b + "/", expires: Date.now() + 1e12, refresh_token: "" }));
    if (!sessionStorage.getItem("t")) { sessionStorage.setItem("t", "1"); for (const k of Object.keys(localStorage)) if (k.startsWith("ss-timeline-card.")) localStorage.removeItem(k);
      for (const [k, v] of Object.entries(prefs ?? {})) localStorage.setItem("ss-timeline-card." + k, JSON.stringify(v)); }
  }, [token, base, prefs]);
  await page.goto(base + path);
  const card = page.locator(tag);
  await card.waitFor({ state: "attached", timeout: 30000 });
  const ev = (fn, arg) => card.evaluate(fn, arg);
  return { browser, page, card, ev };
}
// One line per player: wall (relative to ref), paused, rate, frames decoded, veil, buffer ahead.
export const snap = (ev, ref) => ev((c, ref) => [...c._players.values()].map((p) => {
  const v = p.video, f = p.feed, q = v.getVideoPlaybackQuality?.();
  const ahead = f?.track?.sink ? (f.track.sink.end() - v.currentTime).toFixed(2) : "-";
  return `${c._master === p ? "*" : " "}${p.cameraId}:${(p.wall() - ref).toFixed(2)}${v.paused ? "P" : ">"} r${v.playbackRate} f${q?.totalVideoFrames ?? "?"}/${q?.droppedVideoFrames ?? "?"} ${v.videoWidth}x${v.videoHeight} a${ahead}${f?.ssPaused ? " ssP" : ""}${f?.live ? " L" : ""}${p.loading ? " ld" : ""}${p.veilKind ? " [" + p.veilKind + ":" + p.veil.textContent.trim().replace(/\s+/g, " ").slice(0, 40) + "]" : ""}`;
}).join("  "), ref);
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// A scenario's checks: one "ok" / "FAIL" line each; done() prints the verdict
// and exits 1 if any failed.
export function checks() {
  const fail = [];
  const check = (ok, what) => { console.log(ok ? "ok  " : "FAIL", what); if (!ok) fail.push(what); return ok; };
  const done = async (browser) => {
    console.log(fail.length ? `FAILED: ${fail.join("; ")}` : "PASS");
    await browser?.close();
    process.exit(fail.length ? 1 : 0);
  };
  return { check, done };
}
