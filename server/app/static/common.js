// Shared helpers for the pages: translations, fetch with CSRF, DOM, formats.
// Each page's root element carries data-i18n (its strings), data-csrf and data-locale.
window.DS = (root) => {
  const T = JSON.parse(root.dataset.i18n || "{}");
  const CSRF = root.dataset.csrf;
  const LOCALE = root.dataset.locale || document.documentElement.lang || undefined;

  const t = (key, vars = {}) =>
    (T[key] || key).replace(/\{(\w+)\}/g, (_, k) => (k in vars ? vars[k] : `{${k}}`));
  const has = (key) => key in T;

  // Turn an API error body ({error: code, message, ...}) into text in the page's language.
  const errText = (res) => {
    const d = (res && res.data) || {};
    if (d.error && has(`err.${d.error}`)) return t(`err.${d.error}`, d);
    if (res && res.status === 0) return t("err.network");
    return t("err.http", { status: (res && res.status) || "?" });
  };
  // Stored error ({code, detail, params}) from a transcript or insight.
  const storedErr = (e) => {
    if (!e) return "";
    const text = has(`err.${e.code}`) ? t(`err.${e.code}`, e.params || {}) : t("err.other");
    return text;
  };

  const api = async (method, url, body) => {
    const headers = { "X-CSRF-Token": CSRF };
    const raw = body instanceof Blob || body instanceof FormData;
    if (body !== undefined && !raw) headers["Content-Type"] = "application/json";
    let res;
    try {
      res = await fetch(url, {
        method, headers, credentials: "same-origin",
        body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
      });
    } catch {
      return { ok: false, status: 0, data: null };
    }
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    return { ok: res.ok, status: res.status, data, headers: res.headers };
  };

  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    n.append(...kids.filter((k) => k != null && k !== false));
    return n;
  };

  const dur = (s) => {
    s = Math.floor(s || 0);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  };
  const dateFmt = new Intl.DateTimeFormat(LOCALE, { dateStyle: "medium", timeStyle: "short" });
  const date = (iso) => (iso ? dateFmt.format(new Date(iso)) : "");

  return { t, has, errText, storedErr, api, el, dur, date, CSRF, LOCALE };
};
