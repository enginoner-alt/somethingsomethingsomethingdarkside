// End-to-end SSO test: browser → app → Keycloak (LDAP + TOTP) → app.
// TEST HARNESS ONLY. Needs: Playwright, app on APP_URL, Keycloak on KC_URL,
// OpenLDAP seeded with seed.ldif, Keycloak configured by configure-keycloak.sh.
//
//   node e2e.cjs            (screenshots go to $SHOTS or ./shots)

const { chromium } = require("playwright");
const crypto = require("crypto");
const { execFileSync } = require("child_process");
const fs = require("fs");

const APP = process.env.APP_URL || "http://localhost:8000";
const SHOTS = process.env.SHOTS || "shots";
const PASSWORD = "Passw0rd!";
const LDAP = ["-x", "-H", process.env.LDAP_URL || "ldap://127.0.0.1",
  "-D", "cn=admin,dc=darkside,dc=test", "-w", process.env.LDAP_BIND_PASSWORD || "ldap-admin-pw"];
fs.mkdirSync(SHOTS, { recursive: true });

// --- TOTP (RFC 6238, SHA1, 6 digits, 30 s) ------------------------------------
function base32(s) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const c of s.replace(/[\s=]/g, "").toUpperCase()) bits += alphabet.indexOf(c).toString(2).padStart(5, "0");
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}
function totp(secret, step = Math.floor(Date.now() / 30000)) {
  const msg = Buffer.alloc(8);
  msg.writeBigUInt64BE(BigInt(step));
  const h = crypto.createHmac("sha1", base32(secret)).update(msg).digest();
  const o = h[h.length - 1] & 0xf;
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1e6).padStart(6, "0");
}
// Keycloak refuses a code that was already used, so wait for a fresh window.
const usedSteps = new Map();
async function freshCode(user, secret) {
  while (usedSteps.get(user) === Math.floor(Date.now() / 30000)) await new Promise((r) => setTimeout(r, 1000));
  const step = Math.floor(Date.now() / 30000);
  usedSteps.set(user, step);
  return totp(secret, step);
}

// --- helpers --------------------------------------------------------------------
const secrets = {};
let failures = 0;
let current = null; // page under test, for failure diagnostics
const check = async (ok, label) => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
  if (!ok) {
    failures++;
    if (current) {
      const shot = `${SHOTS}/FAIL-${failures}.png`;
      await current.screenshot({ path: shot }).catch(() => {});
      console.log(`      at ${current.url()} (screenshot ${shot})`);
    }
  }
};

// Click something that submits a form and wait until the browser has actually
// navigated (checking page.url() right after click() is a race).
const submit = (page, selector) => Promise.all([page.waitForNavigation(), page.click(selector)]);

async function ssoLogin(browser, user, shotPrefix) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(`${APP}/login`);
  current = page;
  await submit(page, "text=Sign in with Keycloak");
  await page.fill("#username", user);
  await page.fill("#password", PASSWORD);
  await submit(page, "#kc-login");

  if (await page.locator("#mode-manual").count()) {
    // First login: Keycloak forces TOTP enrolment.
    if (shotPrefix) await page.screenshot({ path: `${SHOTS}/${shotPrefix}-totp-setup.png` });
    await page.click("#mode-manual");
    secrets[user] = (await page.locator("#kc-totp-secret-key").innerText()).trim();
    await page.fill("#totp", await freshCode(user, secrets[user]));
    await submit(page, "#saveTOTPBtn");
  } else if (await page.locator("#otp").count()) {
    await page.fill("#otp", await freshCode(user, secrets[user]));
    await submit(page, "#kc-login");
  }
  return { ctx, page };
}

(async () => {
  const browser = await chromium.launch({ args: ["--no-proxy-server"] });

  // 1. Admin-group user: first login enrols TOTP, lands in the app as admin.
  let { ctx, page } = await ssoLogin(browser, "jdoe", "jdoe");
  await check(page.url() === `${APP}/`, "jdoe: SSO + TOTP enrolment lands on the app");
  const html = await page.content();
  await check(html.includes("Jane Doe") && !html.includes("Jane Doe Doe"), "jdoe: display name comes from LDAP (given name + surname)");
  await check((await page.goto(`${APP}/admin`)).status() === 200, "jdoe: darkside-admins → admin panel allowed");
  await page.screenshot({ path: `${SHOTS}/jdoe-admin.png`, fullPage: true });

  // 2. Logout ends the Keycloak session too.
  await page.goto(`${APP}/`);
  await submit(page, "button:has-text('Log out')");
  if (await page.locator("#kc-logout").count()) await submit(page, "#kc-logout");
  await page.waitForURL(`${APP}/login`);
  await submit(page, "text=Sign in with Keycloak");
  await check((await page.locator("#username").count()) === 1, "logout: IdP session ended (password asked again)");
  await ctx.close();

  // 3. Second login asks for the TOTP code (MFA really enforced).
  ({ ctx, page } = await ssoLogin(browser, "jdoe"));
  await check(page.url() === `${APP}/`, "jdoe: second login with password + TOTP code");
  await ctx.close();

  // 4. Regular user: logged in, but no admin.
  ({ ctx, page } = await ssoLogin(browser, "asmith", "asmith"));
  await check(page.url() === `${APP}/`, "asmith: SSO login works");
  await check((await page.goto(`${APP}/admin`)).status() === 403, "asmith: not in darkside-admins → 403 on /admin");
  await ctx.close();

  // 5. Valid LDAP account outside the required group is refused by the app.
  ({ ctx, page } = await ssoLogin(browser, "mallory", "mallory"));
  await check((await page.content()).includes("isn't allowed to use this app"), "mallory: not in darkside-users → refused");
  await page.screenshot({ path: `${SHOTS}/mallory-refused.png` });
  await ctx.close();

  // 6. Removing a user from the group in LDAP locks them out at next login.
  execFileSync("ldapmodify", LDAP, {
    input: "dn: cn=darkside-users,ou=groups,dc=darkside,dc=test\nchangetype: modify\ndelete: member\nmember: uid=asmith,ou=people,dc=darkside,dc=test\n",
  });
  ({ ctx, page } = await ssoLogin(browser, "asmith"));
  await check((await page.content()).includes("isn't allowed to use this app"), "asmith: removed from group in LDAP → refused");
  await ctx.close();

  // 7. Deleting a user from LDAP: Keycloak rejects the password outright.
  execFileSync("ldapdelete", [...LDAP, "uid=mallory,ou=people,dc=darkside,dc=test"]);
  ctx = await browser.newContext();
  page = current = await ctx.newPage();
  await page.goto(`${APP}/login`);
  await submit(page, "text=Sign in with Keycloak");
  await page.fill("#username", "mallory");
  await page.fill("#password", PASSWORD);
  await submit(page, "#kc-login");
  await check((await page.locator("#input-error, .kc-feedback-text, [id*=error]").count()) > 0 && !page.url().startsWith(APP),
    "mallory: deleted from LDAP → Keycloak refuses login");
  await ctx.close();

  // 8. A local account can't sneak in with an SSO-only username, and the
  //    break-glass admin works through the local form.
  ctx = await browser.newContext();
  page = current = await ctx.newPage();
  await page.goto(`${APP}/login`);
  await page.fill("input[name=username]", "jdoe");
  await page.fill("input[name=password]", PASSWORD);
  await submit(page, "form[action='/login'] button");
  await check((await page.content()).includes("Wrong username or password"), "jdoe: SSO user has no local password");
  await page.fill("input[name=username]", "admin");
  await page.fill("input[name=password]", "admin-password-123");
  await submit(page, "form[action='/login'] button");
  await check(page.url() === `${APP}/`, "admin: break-glass local login works");
  await page.goto(`${APP}/admin`);
  await page.screenshot({ path: `${SHOTS}/admin-users.png`, fullPage: true });
  await ctx.close();

  await browser.close();
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nAll checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(2);
});
