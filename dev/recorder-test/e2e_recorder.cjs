// Browser end-to-end test of recording + upload, with a fake microphone.
// TEST HARNESS ONLY.
//
// Chromium plays MARKERS_WAV (made by `markers.py make`) as its microphone.
// Each scenario logs in as an AD user, records through the real page,
// downloads what the server stored, and checks it with `markers.py check`.
//
//   APP_URL=http://localhost:8000 MARKERS_WAV=/path/markers.wav OUT=/tmp/out \
//   LOGIN_USER=asmith LOGIN_PASSWORD='Passw0rd-Test!' OTHER_USER=jdoe node e2e_recorder.cjs
//
// The app must run with RECORDER_CHUNK_SECONDS=1 and RECORDING_STALE_SECONDS=5.

const { chromium } = require("playwright");
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const APP = process.env.APP_URL || "http://localhost:8000";
const WAV = process.env.MARKERS_WAV;
const OUT = process.env.OUT || "out";
const PY = process.env.PYTHON || "python3";
const MARKERS = path.join(__dirname, "markers.py");
// Defaults: the Samba AD test users (dev/ad-test). CI uses local accounts.
const USER = process.env.LOGIN_USER || "asmith";
const PASS = process.env.LOGIN_PASSWORD || "Passw0rd-Test!";
const OTHER = process.env.OTHER_USER || "jdoe";
const OTHER_PASS = process.env.OTHER_PASSWORD || PASS;
const STALE_S = Number(process.env.STALE_SECONDS || 5);
fs.mkdirSync(OUT, { recursive: true });

let failures = 0;
const results = [];
const check = (ok, label, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${!ok && detail ? `  [${detail}]` : ""}`);
  if (!ok) failures += 1;
  results.push({ ok, label, detail });
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function newSession(browser, user = USER, pass = PASS) {
  const ctx = await browser.newContext({ permissions: ["microphone"] });
  const page = await ctx.newPage();
  await page.goto(`${APP}/login`);
  await page.fill("input[name=username]", user);
  await page.fill("input[name=password]", pass);
  await Promise.all([page.waitForNavigation(), page.click("form[action='/login'] button")]);
  return { ctx, page };
}

async function readyToRecord(page) {
  await page.check("#consent");
  await page.click("#mic-allow");
  await page.waitForFunction(() => !document.getElementById("btn-record").disabled, null, { timeout: 15000 });
}

async function waitPhase(page, phases, timeout = 60000) {
  await page.waitForFunction((ps) => ps.includes(window.__recorder.phase), phases, { timeout });
  return page.evaluate(() => ({ ...window.__recorder, queue: window.__recorder.queue.length }));
}

async function recordFor(page, seconds) {
  await page.click("#btn-record");
  await waitPhase(page, ["recording"]);
  await sleep(seconds * 1000);
}

async function stopAndWait(page) {
  await page.click("#btn-stop");
  return waitPhase(page, ["done", "error"]);
}

async function recordingById(page, id) {
  const list = await page.evaluate(async () => (await fetch("/api/recordings")).json());
  return list.find((r) => r.id === id);
}

async function downloadAndCheck(ctx, rec, name, pauses = 0) {
  const res = await ctx.request.get(`${APP}${rec.audio_url}`);
  const file = path.join(OUT, `${name}.${rec.mime_type.split("/")[1]}`);
  fs.writeFileSync(file, await res.body());
  try {
    return JSON.parse(execFileSync(PY, [MARKERS, "check", file, "--pauses", String(pauses)]).toString());
  } catch (e) {
    return JSON.parse(e.stdout.toString() || '{"ok":false}');
  }
}

(async () => {
  const browser = await chromium.launch({
    args: [
      "--no-proxy-server",
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${WAV}`,
      "--autoplay-policy=no-user-gesture-required",
    ],
  });

  // 1. Normal recording, logged in as an AD user.
  let { ctx, page } = await newSession(browser);
  check(page.url() === `${APP}/`, `${USER} logs in and lands on the recorder`);
  check((await ctx.request.get(`${APP}/admin`)).status() === 403, `${USER} is not an admin`);
  await readyToRecord(page);
  const devices = await page.$$eval("#mic-select option", (os) => os.map((o) => o.textContent));
  check(devices.length >= 1, "mic picker lists the (fake) microphone", devices.join(", "));
  await recordFor(page, 6);
  let st = await stopAndWait(page);
  check(st.phase === "done" && st.acked === st.nextSeq && st.nextSeq >= 5, "normal: all chunks acked, finalized",
    JSON.stringify(st));
  let rec = await recordingById(page, st.recId);
  check(rec && rec.status === "uploaded", "normal: server status 'uploaded'", JSON.stringify(rec));
  let m = await downloadAndCheck(ctx, rec, "1-normal");
  check(m.ok, `normal: audio intact (${m.markers?.length} one-second markers, ${m.duration_s} s)`, JSON.stringify(m));
  check(Math.abs(rec.duration_seconds - m.duration_s) < 0.5, "normal: server-measured duration matches",
    `${rec.duration_seconds} vs ${m.duration_s}`);
  console.log(`info  channels stored: ${rec.channels}; right channel distinct: ${m.stereo_preserved} (${m.right_channel_hz} Hz)`);
  const normalId = rec.id, normalUrl = rec.audio_url;
  await ctx.close();

  // 2. Network drop mid-recording.
  ({ ctx, page } = await newSession(browser));
  await readyToRecord(page);
  await recordFor(page, 2);
  await ctx.setOffline(true);
  await sleep(4000);
  const during = await page.evaluate(() => ({ acked: window.__recorder.acked, total: window.__recorder.nextSeq, retries: window.__recorder.retries }));
  await ctx.setOffline(false);
  await sleep(2000);
  st = await stopAndWait(page);
  check(during.total - during.acked >= 2, "offline: chunks queue up while offline",
    JSON.stringify(during));
  check(st.phase === "done" && st.acked === st.nextSeq, "offline: queue drained after reconnect, finalized", JSON.stringify(st));
  rec = await recordingById(page, st.recId);
  m = await downloadAndCheck(ctx, rec, "2-offline");
  check(m.ok, `offline: audio intact across the 4 s drop (${m.duration_s} s)`, JSON.stringify(m));
  await ctx.close();

  // 3. Flaky network: a dropped request, a lost response (server stored it), a 500.
  ({ ctx, page } = await newSession(browser));
  const seen = {};
  await page.route(/\/api\/recordings\/[^/]+\/chunks\/\d+$/, async (route) => {
    const seq = Number(route.request().url().split("/").pop());
    seen[seq] = (seen[seq] || 0) + 1;
    if (seq === 1 && seen[seq] === 1) return route.abort("connectionreset");
    if (seq === 2 && seen[seq] === 1) { await route.fetch(); return route.abort("connectionreset"); }
    if (seq === 3 && seen[seq] === 1) return route.fulfill({ status: 500, body: "boom" });
    return route.continue();
  });
  await readyToRecord(page);
  await recordFor(page, 6);
  st = await stopAndWait(page);
  check(st.phase === "done" && st.retries >= 3, `flaky: recovered after ${st.retries} retries`, JSON.stringify(st));
  rec = await recordingById(page, st.recId);
  m = await downloadAndCheck(ctx, rec, "3-flaky");
  check(m.ok, "flaky: audio intact (chunk 2 stored once despite being sent twice)", JSON.stringify(m));
  await ctx.close();

  // 4. Pause / resume.
  ({ ctx, page } = await newSession(browser));
  await readyToRecord(page);
  await recordFor(page, 2.5);
  await page.click("#btn-pause");
  await sleep(3000);
  await page.click("#btn-pause");
  await sleep(2500);
  st = await stopAndWait(page);
  rec = await recordingById(page, st.recId);
  m = await downloadAndCheck(ctx, rec, "4-pause", 1);
  check(m.ok && m.jumps.length === 1, `pause: one jump where the pause was, rest intact (${m.duration_s} s)`, JSON.stringify(m));
  check(m.duration_s < 6.5, "pause: paused time not recorded", `${m.duration_s} s`);
  await ctx.close();

  // 5. Tab closed mid-recording -> 'interrupted', what arrived is kept.
  ({ ctx, page } = await newSession(browser));
  await readyToRecord(page);
  await recordFor(page, 4);
  const lost = await page.evaluate(() => window.__recorder.recId);
  await page.close({ runBeforeUnload: false });
  await sleep((STALE_S + 2) * 1000);
  page = await ctx.newPage();
  await page.goto(`${APP}/`);
  rec = await recordingById(page, lost);
  check(rec && rec.status === "interrupted" && rec.audio_url, "tab closed: recording kept as 'interrupted'", JSON.stringify(rec));
  if (rec && rec.audio_url) {
    m = await downloadAndCheck(ctx, rec, "5-interrupted");
    check(m.ok && m.duration_s >= 2, `tab closed: the part that arrived is intact (${m.duration_s} s)`, JSON.stringify(m));
  }

  // 6. Upload an existing file through the page.
  const mp3 = path.join(OUT, "existing.mp3");
  execFileSync("ffmpeg", ["-v", "error", "-y", "-stream_loop", "1", "-i", WAV, "-t", "5", "-c:a", "libmp3lame", mp3]);
  await page.check("#consent");
  await page.setInputFiles("#upload-file", mp3);
  await page.click("#upload-form button");
  await page.waitForFunction(() => /Uploaded|Yüklendi/.test(document.getElementById("upload-status").textContent), null, { timeout: 30000 });
  const list = await page.evaluate(async () => (await fetch("/api/recordings")).json());
  rec = list.find((r) => r.source === "upload");
  m = rec ? await downloadAndCheck(ctx, rec, "6-upload") : { ok: false };
  check(rec && m.ok, "existing mp3 uploaded through the page, audio intact", JSON.stringify(m));

  // 7. Safari-style fragmented MP4/AAC chunks via the API.
  const fmp4 = path.join(OUT, "safari-like.mp4");
  execFileSync("ffmpeg", ["-v", "error", "-y", "-stream_loop", "1", "-i", WAV, "-t", "5", "-c:a", "aac",
    "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "1000000", fmp4]);
  const bytes = fs.readFileSync(fmp4);
  const csrf = await page.$eval("#recorder-app", (e) => e.dataset.csrf);
  const headers = { "X-CSRF-Token": csrf };
  const created = await (await ctx.request.post(`${APP}/api/recordings`, {
    headers, data: { mime_type: "audio/mp4", consent: true, title: "safari-like" } })).json();
  const n = 5, size = Math.ceil(bytes.length / n);
  for (let i = 0; i < n; i += 1) {
    await ctx.request.put(`${APP}/api/recordings/${created.id}/chunks/${i}`, {
      headers: { ...headers, "Content-Type": "application/octet-stream" }, data: bytes.subarray(i * size, (i + 1) * size) });
  }
  const fin = await (await ctx.request.post(`${APP}/api/recordings/${created.id}/finalize`, { headers, data: { last_seq: n - 1 } })).json();
  m = fin.audio_url ? await downloadAndCheck(ctx, fin, "7-safari-like") : { ok: false };
  check(fin.status === "uploaded" && m.ok, "Safari-style fragmented MP4 chunks: reassembled, audio intact", JSON.stringify({ fin: fin.status, m }));
  await ctx.close();

  // 8. Another AD user can't reach the first user's recording.
  ({ ctx, page } = await newSession(browser, OTHER, OTHER_PASS));
  const peek = await ctx.request.get(`${APP}${normalUrl}`);
  const theirs = await page.evaluate(async () => (await fetch("/api/recordings")).json());
  check(peek.status() === 404 && !theirs.some((r) => r.id === normalId), `${OTHER} can't see or download ${USER}'s recording`);
  await ctx.close();

  await browser.close();
  fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify(results, null, 2));
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nAll checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
