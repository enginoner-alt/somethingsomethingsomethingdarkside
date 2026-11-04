// Recording page: transcript, speakers, insights. Polls while work is running.
(() => {
  const app = document.getElementById("recording-app");
  if (!app) return;
  const ID = app.dataset.id, CSRF = app.dataset.csrf;
  const T = JSON.parse(app.dataset.i18n || "{}");
  const t = (k, v = {}) => (T[k] || k).replace(/\{(\w+)\}/g, (_, x) => (x in v ? v[x] : `{${x}}`));
  const $ = (id) => document.getElementById(id);
  const player = $("player");
  let speakers = {};

  const api = async (method, url, body) => {
    const res = await fetch(url, {
      method, credentials: "same-origin",
      headers: { "X-CSRF-Token": CSRF, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty */ }
    return { ok: res.ok, status: res.status, data };
  };
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
    }
    n.append(...kids);
    return n;
  };
  const ts = (s) => {
    s = Math.floor(s || 0);
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  };
  const colour = (label) => `spk-${(parseInt((label || "").replace(/\D/g, ""), 10) || 0) % 6}`;
  const who = (label) => (label ? speakers[label] || label : t("tx.unknown_speaker"));

  // --- transcript ------------------------------------------------------------------
  let txTimer = null;
  const loadTranscript = async () => {
    const res = await api("GET", `/api/recordings/${ID}/transcript`);
    if (!res.ok) return;
    const tr = res.data.transcript;
    $("rec-title").textContent = res.data.recording.title;
    const badge = $("tx-status");
    badge.textContent = t(`tx.status_${tr.status}`);
    badge.className = `badge ${tr.status === "done" ? "done" : tr.status === "failed" ? "failed" : "running"}`;
    $("tx-error").hidden = !tr.error;
    $("tx-error").textContent = tr.error || "";
    speakers = Object.fromEntries((tr.speakers || []).map((s) => [s.label, s.name]));
    renderSpeakers(tr.speakers || []);
    $("segments").replaceChildren(...(tr.segments || []).map((s) =>
      el("div", { class: "seg", "data-start": s.start ?? "", onclick: () => seek(s.start) },
        el("span", { class: "mono muted" }, ts(s.start)), " ",
        el("strong", { class: colour(s.speaker) }, who(s.speaker)), " ",
        el("span", {}, s.text))));
    if (!tr.segments?.length && tr.status === "none") $("segments").replaceChildren(el("p", { class: "muted" }, t("tx.none")));
    clearTimeout(txTimer);
    if (tr.status === "queued" || tr.status === "running") txTimer = setTimeout(loadTranscript, 2000);
    document.querySelectorAll("#insight-buttons button, #ask-form button").forEach((b) => { b.disabled = tr.status !== "done"; });
  };
  const seek = (start) => { if (player && start != null) { player.currentTime = start; player.play().catch(() => {}); } };

  const renderSpeakers = (list) => {
    if (!list.length) { $("speakers").replaceChildren(); return; }
    const inputs = list.map((s) => el("label", {}, el("span", { class: colour(s.label) }, s.label),
      el("input", { "data-label": s.label, value: s.name || "", placeholder: t("tx.speaker_name") })));
    $("speakers").replaceChildren(...inputs, el("button", { onclick: saveSpeakers }, t("tx.save_names")));
  };
  const saveSpeakers = async () => {
    const names = {};
    document.querySelectorAll("#speakers input[data-label]").forEach((i) => { names[i.dataset.label] = i.value; });
    await api("PATCH", `/api/recordings/${ID}/speakers`, { names });
    loadTranscript();
  };

  $("tx-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const res = await api("POST", `/api/recordings/${ID}/transcribe`, {
      language: f.get("language") || null, channel: f.get("channel") || null });
    if (!res.ok) { $("tx-error").hidden = false; $("tx-error").textContent = res.data?.message || `HTTP ${res.status}`; }
    loadTranscript();
  });

  // --- insights ----------------------------------------------------------------------
  let insTimer = null;
  const loadInsights = async () => {
    const res = await api("GET", `/api/recordings/${ID}/insights`);
    if (!res.ok) return;
    $("insights").replaceChildren(...res.data.map(renderInsight));
    clearTimeout(insTimer);
    if (res.data.some((i) => i.status === "queued" || i.status === "running")) insTimer = setTimeout(loadInsights, 1500);
    else if (res.data.some((i) => i.kind === "subject")) loadTranscript(); // title may have changed
  };
  const renderInsight = (i) => {
    const head = el("div", { class: "rec-head" },
      el("strong", {}, i.kind === "question" ? `${t("tx.kind_question")}: ${i.question}` : t(`tx.kind_${i.kind}`)), " ",
      el("span", { class: `badge ${i.status === "done" ? "done" : i.status === "failed" ? "failed" : "running"}` }, t(`tx.status_${i.status}`)));
    const body = el("div", { class: "answer" });
    if (i.status === "failed") body.append(el("p", { class: "error" }, i.error || ""));
    else if (i.kind === "speakers" && i.data?.suggestions) {
      const sug = i.data.suggestions;
      body.append(sug.length
        ? el("ul", {}, ...sug.map((s) => el("li", {}, `${s.label} → ${s.name}`, s.evidence ? el("span", { class: "muted" }, ` (${s.evidence})`) : "")))
        : el("p", { class: "muted" }, t("tx.no_names")));
      if (sug.length) body.append(el("button", { onclick: async () => { await api("POST", `/api/insights/${i.id}/apply-speakers`); loadTranscript(); } }, t("tx.apply_names")));
    } else if (i.answer) body.append(el("pre", { class: "answer-text" }, i.answer));
    const foot = el("div", { class: "row small muted" },
      i.created_at ? new Date(i.created_at).toLocaleString() : "", i.model ? ` · ${i.model}` : "",
      el("button", { class: "link", onclick: () => ask(i.kind, i.question) }, t("tx.rerun")),
      el("button", { class: "link", onclick: async () => { await api("DELETE", `/api/insights/${i.id}`); loadInsights(); } }, t("rec.delete")));
    return el("div", { class: "rec-row", "data-kind": i.kind, "data-status": i.status }, head, body, foot);
  };
  const ask = async (kind, question = null) => {
    const res = await api("POST", `/api/recordings/${ID}/insights`, { kind, question });
    $("insight-msg").textContent = res.ok ? "" : res.data?.message || `HTTP ${res.status}`;
    loadInsights();
  };
  document.querySelectorAll("#insight-buttons button").forEach((b) => b.addEventListener("click", () => ask(b.dataset.kind)));
  $("ask-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const q = ev.target.question.value.trim();
    if (q) { ask("question", q); ev.target.reset(); }
  });

  if (player) player.addEventListener("timeupdate", () => {
    const now = player.currentTime;
    document.querySelectorAll(".seg").forEach((s) => {
      const start = parseFloat(s.dataset.start), next = s.nextElementSibling ? parseFloat(s.nextElementSibling.dataset.start) : Infinity;
      s.classList.toggle("playing", now >= start && now < next);
    });
  });

  loadTranscript();
  loadInsights();
})();
