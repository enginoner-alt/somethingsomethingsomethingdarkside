// Browser end-to-end test of M3: upload -> auto-transcribe -> transcript ->
// insight buttons -> names applied -> free question. TEST HARNESS ONLY.
// Needs the app + worker-asr + worker-llm running, Services pointing at the
// fakes (dev/fakes), and a user (default: Samba AD user asmith).
//
//   APP_URL=http://localhost:8000 AUDIO=dev/test-audio/four-channel-tones.wav \
//   LOGIN_USER=asmith LOGIN_PASSWORD='Passw0rd-Test!' SHOT=/tmp/m3.png node e2e_m3.cjs

const { chromium } = require("playwright");
const APP = process.env.APP_URL || "http://localhost:8000";
const AUDIO = process.env.AUDIO;
const USER = process.env.LOGIN_USER || "asmith";
const PASS = process.env.LOGIN_PASSWORD || "Passw0rd-Test!";
let failures = 0;
const check = (ok, label, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${!ok && detail ? `  [${detail}]` : ""}`);
  if (!ok) failures += 1;
};

(async () => {
  const browser = await chromium.launch({ args: ["--no-proxy-server"] });
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 1000 } });
  const page = await ctx.newPage();
  await page.goto(`${APP}/login`);
  await page.fill("input[name=username]", USER);
  await page.fill("input[name=password]", PASS);
  await Promise.all([page.waitForNavigation(), page.click("form[action='/login'] button")]);
  check(page.url() === `${APP}/`, `${USER} logged in`);

  // Upload the 4-channel file through the page; auto-transcribe kicks in.
  await page.click(".upload-box summary");
  await page.check("#consent");
  await page.setInputFiles("#upload-file", AUDIO);
  await page.click("#upload-form button");
  await page.waitForFunction(() => /Uploaded|Yüklendi/.test(document.getElementById("upload-status").textContent), null, { timeout: 30000 });
  await page.waitForSelector(".rec-row a[href^='/recordings/']");
  await Promise.all([page.waitForNavigation(), page.click(".rec-row a[href^='/recordings/']")]);
  check(/\/recordings\/[0-9a-f-]+$/.test(page.url()), "opened the recording page");

  await page.waitForFunction(() => document.querySelectorAll(".seg").length > 0, null, { timeout: 60000 });
  const segs = await page.$$eval(".seg", (s) => s.map((x) => x.textContent));
  check(segs.length === 6, `transcript shown automatically (${segs.length} segments)`);
  check(segs.some((s) => s.includes("SPEAKER_00")) && segs.some((s) => s.includes("SPEAKER_01")), "speaker labels shown");
  const status = await page.textContent("#tx-status");
  check(/done|tamam/.test(status), `transcript status: ${status}`);

  // Every default insight button.
  const want = {
    summary: /FAKE SUMMARY/,
    subject: /Ofis taşınma planlaması/,
    action_items: /Ayse: send the moving checklist/,
    decisions: /Sign the office lease/,
    speakers: /Ayşe · SPEAKER_00[\s\S]*Mehmet · SPEAKER_01/,
  };
  for (const kind of Object.keys(want)) {
    await page.click(`#insight-buttons button[data-kind=${kind}]`);
    await page.waitForSelector(`.insight[data-kind=${kind}][data-status=done], .insight[data-kind=${kind}][data-status=failed]`, { timeout: 30000 });
    const text = await page.textContent(`.insight[data-kind=${kind}]`);
    check(want[kind].test(text), `insight '${kind}' answered and saved`, text.slice(0, 160));
  }
  await page.waitForFunction(() => document.getElementById("rec-title").textContent.includes("Ofis"), null, { timeout: 10000 }).catch(() => {});
  check((await page.textContent("#rec-title")).includes("Ofis taşınma planlaması"), "meeting subject became the recording's title");

  // Apply the suggested names -> transcript shows them.
  await page.click(".insight[data-kind=speakers] button:not(.link)");
  await page.waitForFunction(() => [...document.querySelectorAll(".seg strong")].some((s) => s.textContent === "Ayşe"), null, { timeout: 10000 });
  const who = await page.$$eval(".seg strong", (s) => [...new Set(s.map((x) => x.textContent))]);
  check(who.includes("Ayşe") && who.includes("Mehmet"), `names applied in the transcript (${who.join(", ")})`);

  // Everything persisted: reload and the insights are all still there.
  await page.reload();
  await page.waitForSelector(".insight[data-kind=summary]");
  const kinds = await page.$$eval("#insights .insight", (r) => r.map((x) => x.dataset.kind));
  check(["summary", "subject", "action_items", "decisions", "speakers"].every((k) => kinds.includes(k)),
    `after reload all ${kinds.length} insights are still there (saved in the database)`);
  const recUrl = page.url();

  // Ask: a question opens a chat about this meeting; a follow-up remembers it.
  await page.fill("#ask-form input[name=question]", "Ne zaman imzalanacak?");
  await Promise.all([page.waitForURL(/\/ask\/\d+$/), page.click("#ask-form button")]);
  await page.waitForSelector(".msg.assistant[data-status=done]", { timeout: 30000 });
  let answers = await page.$$eval(".msg.assistant", (m) => m.map((x) => x.textContent));
  check(/FAKE CHAT ANSWER to: Ne zaman imzalanacak\? \(meetings: 1, earlier turns: 0\)/.test(answers[0]),
    "Ask: question about this meeting answered", answers[0]);
  await page.fill("#followup-q", "Kim arayacak?");
  await page.press("#followup-q", "Enter");
  await page.waitForFunction(() => document.querySelectorAll(".msg.assistant[data-status=done]").length === 2, null, { timeout: 30000 });
  answers = await page.$$eval(".msg.assistant", (m) => m.map((x) => x.textContent));
  check(/Kim arayacak\? \(meetings: 1, earlier turns: 2\)/.test(answers[1]), "Ask: follow-up sent with the earlier turns", answers[1]);
  await page.reload();
  await page.waitForSelector(".msg.assistant[data-status=done]");
  check((await page.$$(".msg")).length === 4, "Ask: the conversation is saved");

  // Ask across meetings: pick two on the recordings list.
  await page.goto(`${APP}/`);
  await page.click(".upload-box summary");
  await page.check("#consent");
  await page.setInputFiles("#upload-file", AUDIO);
  await page.click("#upload-form button");
  await page.waitForFunction(() => /Uploaded|Yüklendi/.test(document.getElementById("upload-status").textContent), null, { timeout: 30000 });
  // Wait until the new upload (the newest recording) has its transcript.
  for (let i = 0; i < 60; i += 1) {
    const ready = await page.evaluate(async () => {
      const [newest] = await (await fetch("/api/recordings?limit=1")).json();
      return (await (await fetch("/api/chats/sources")).json()).meetings.some((m) => m.id === newest.id);
    });
    if (ready) break;
    await page.waitForTimeout(1000);
  }
  await page.reload();
  await page.waitForSelector(".rec-row input.pick");
  // Locators, not element handles: the list may redraw between the two clicks.
  const boxes = page.locator(".rec-row input.pick");
  await boxes.nth(0).check(); await boxes.nth(1).check();
  await page.waitForFunction(() => (document.getElementById("ask-selected").getAttribute("href") || "").includes(","));
  await Promise.all([page.waitForURL(/\/ask\?with=/), page.click("#ask-selected")]);
  await page.waitForSelector("#source-list .source");
  const budget = await page.textContent("#budget-text");
  check(/^2 /.test(budget), "Ask: the two picked meetings are preselected, with the context meter", budget);
  await page.fill("#new-q", "Ortak noktalar neler?");
  await Promise.all([page.waitForURL(/\/ask\/\d+$/), page.click("#new-chat-btn")]);
  await page.waitForSelector(".msg.assistant[data-status=done]", { timeout: 30000 });
  const multi = await page.textContent(".msg.assistant");
  check(/meetings: 2/.test(multi), "Ask: one chat over two meetings", multi);
  await page.goto(recUrl);
  await page.waitForSelector(".seg");

  // Transcribe again: channel 2 only, English.
  await page.click("details summary");
  await page.selectOption("#tx-form select[name=language]", "en");
  await page.selectOption("#tx-form select[name=channel]", "2");
  await page.click("#tx-form button");
  await page.waitForFunction(() => [...document.querySelectorAll(".seg")].some((s) => s.textContent.includes("Good morning")), null, { timeout: 60000 });
  const after = await page.$$eval(".seg strong", (s) => [...new Set(s.map((x) => x.textContent))]);
  check(after.includes("Ayşe"), "re-transcribed (English, channel 2); speaker names kept");

  if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT, fullPage: true });
  await browser.close();
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nAll checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
