// Run with Keycloak STOPPED: the SSO button must fail gracefully and the
// break-glass admin must still get in with the local form.
// TEST HARNESS ONLY.   node e2e-idp-down.cjs

const { chromium } = require("playwright");

const APP = process.env.APP_URL || "http://localhost:8000";
const ADMIN_PASSWORD = process.env.BOOTSTRAP_ADMIN_PASSWORD || "admin-password-123";

(async () => {
  let failures = 0;
  const check = (ok, label) => {
    console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
    if (!ok) failures++;
  };
  const browser = await chromium.launch({ args: ["--no-proxy-server"] });
  let page = await browser.newPage();
  await page.goto(`${APP}/login`);
  // Two possible outcomes, both fine:
  // - app started while the IdP was down → app shows its own message;
  // - app already knew the IdP → it redirects there and the *browser* shows a
  //   connection error (the app is no longer involved at that point).
  let appMessage = false;
  try {
    await Promise.all([page.waitForNavigation(), page.click("text=Sign in with Keycloak")]);
    appMessage = (await page.content()).includes("sign-in service is unreachable");
    if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT });
  } catch (e) {
    console.log(`info  browser could not reach the IdP (${e.message.split("\n")[0]})`);
    page = await browser.newPage();
    await page.goto(`${APP}/login`);
  }
  console.log(`info  app-side message shown: ${appMessage}`);

  await page.fill("input[name=username]", "admin");
  await page.fill("input[name=password]", ADMIN_PASSWORD);
  await page.click("form[action='/login'] button");
  check(page.url() === `${APP}/`, "IdP down: break-glass admin logs in locally");

  await browser.close();
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nAll checks passed");
  process.exit(failures ? 1 : 0);
})();
