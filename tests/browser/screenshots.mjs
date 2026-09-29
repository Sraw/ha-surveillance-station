// The README's pictures: the real card on a real installation, with every camera
// picture (the video, the event thumbnails) replaced by a drawn scene, so that no
// home is published. Writes to /pw/shots/ (tests/browser/shots/).
//   tests/browser/run.sh screenshots.mjs [name ...]      names: live grid phone mute
import fs from "fs";
import { open, sleep } from "./harness.mjs";

fs.mkdirSync("/pw/shots", { recursive: true });
const want = process.argv.slice(2);

// Draws a scene into the page: a house, a drive, trees, and (by seed) a car or a person.
// Runs in the browser; returns a JPEG data URL.
function drawScene(seed) {
  const [w, h] = [1280, 720];
  const k = document.createElement("canvas");
  k.width = w;
  k.height = h;
  const x = k.getContext("2d");
  const night = seed % 4 === 3;
  const sky = x.createLinearGradient(0, 0, 0, h * 0.55);
  sky.addColorStop(0, night ? "#0b1530" : "#6fa8dc");
  sky.addColorStop(1, night ? "#24365e" : "#cfe6f7");
  x.fillStyle = sky;
  x.fillRect(0, 0, w, h);
  x.fillStyle = night ? "#1b2a1d" : "#5c8a4a";
  x.fillRect(0, h * 0.55, w, h * 0.45);
  x.fillStyle = night ? "#3a3a44" : "#9a9a9a"; // the drive
  x.beginPath();
  x.moveTo(w * (0.35 + 0.05 * (seed % 3)), h * 0.55);
  x.lineTo(w * (0.55 + 0.05 * (seed % 3)), h * 0.55);
  x.lineTo(w * 0.85, h);
  x.lineTo(w * 0.15, h);
  x.fill();
  const hx = w * (0.08 + 0.12 * (seed % 3));
  x.fillStyle = night ? "#3d3530" : "#d8c3a5"; // the house
  x.fillRect(hx, h * 0.28, w * 0.3, h * 0.3);
  x.fillStyle = night ? "#2a1f1c" : "#8c4a3a";
  x.beginPath();
  x.moveTo(hx - 20, h * 0.28);
  x.lineTo(hx + w * 0.15, h * 0.12);
  x.lineTo(hx + w * 0.3 + 20, h * 0.28);
  x.fill();
  x.fillStyle = night ? "#e8c96a" : "#a9c9e0";
  for (const dx of [0.05, 0.19]) x.fillRect(hx + w * dx, h * 0.36, w * 0.06, h * 0.1);
  for (const [cx, r] of [[w * 0.82, 90], [w * 0.93, 70], [w * 0.05, 80]]) {
    x.fillStyle = night ? "#142016" : "#3e6b34";
    x.beginPath();
    x.arc(cx, h * 0.5, r, 0, 7);
    x.fill();
    x.fillStyle = night ? "#2a2018" : "#6b4a2b";
    x.fillRect(cx - 8, h * 0.5, 16, 90);
  }
  if (seed % 2 === 0) { // a car
    const cx = w * 0.5;
    x.fillStyle = "#b3202a";
    x.fillRect(cx - 110, h * 0.72, 220, 60);
    x.fillRect(cx - 70, h * 0.66, 130, 40);
    x.fillStyle = "#111";
    for (const dx of [-70, 70]) { x.beginPath(); x.arc(cx + dx, h * 0.72 + 62, 22, 0, 7); x.fill(); }
  } else { // a person
    const px = w * 0.62;
    x.fillStyle = "#2b4a7a";
    x.fillRect(px - 16, h * 0.62, 32, 70);
    x.fillStyle = "#e0b48a";
    x.beginPath();
    x.arc(px, h * 0.6, 16, 0, 7);
    x.fill();
    x.fillStyle = "#222";
    x.fillRect(px - 14, h * 0.62 + 70, 12, 40);
    x.fillRect(px + 2, h * 0.62 + 70, 12, 40);
  }
  return k.toDataURL("image/jpeg", 0.8);
}

// Keep every picture of the card a drawn one, however often it draws again.
function fakePictures(card, draw) {
  const apply = () => {
    let i = 0;
    for (const p of card._players?.values() ?? []) {
      const seed = p.cameraId;
      p.video.style.visibility = "hidden";
      p.still?.classList.remove("show");
      p.veil.style.display = "none";
      const vp = p.el.querySelector(".vp");
      if (vp.dataset.fake !== String(seed)) {
        vp.dataset.fake = String(seed);
        vp.style.background = `#000 url(${draw(seed)}) center / cover`;
      }
      i++;
    }
    let n = 0;
    for (const img of card.shadowRoot.querySelectorAll(".thumb img")) {
      img.classList.remove("bad");
      if (!img.dataset.fake) {
        img.dataset.fake = "1";
        img.src = draw(n++ + 1);
      }
    }
  };
  apply();
  new MutationObserver(apply).observe(card.shadowRoot, { childList: true, subtree: true });
  setInterval(apply, 1000);
}

// No picture is decoded (4K H.265 of every camera at once is a lot for a rig): the stream's
// data goes nowhere, and what the video would show is drawn.
const noDecode = () => { SourceBuffer.prototype.appendBuffer = () => {}; };

const jobs = {
  live: { file: "live.png", opts: { w: 1400, h: 900, prefs: { grid: false, events: true } } },
  grid: { file: "grid.png", opts: { w: 1400, h: 900, prefs: { grid: true, events: false } } },
  phone: { file: "phone.png", opts: { w: 390, h: 844, mobile: true, prefs: { grid: false, events: false } } },
  mute: { file: "mute.png", opts: { w: 700, h: 700, path: "/ss-playback/notifications", tag: "ss-mute-card" } },
};
for (const [name, { file, opts }] of Object.entries(jobs)) {
  if (want.length && !want.includes(name)) continue;
  const { browser, card } = await open({ ...opts, init: name === "mute" ? null : noDecode });
  if (name !== "mute") {
    await sleep(8000);
    await card.evaluate(new Function("card", `${drawScene}\n${fakePictures}\nfakePictures(card, drawScene);`)); // both run in the page
    await sleep(2500);
  } else await sleep(3000);
  await card.screenshot({ path: `/pw/shots/${file}` });
  console.log("wrote", file);
  await browser.close();
}
