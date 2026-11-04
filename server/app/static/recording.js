// Recording page: transcript, speakers, insights. Polls while work is running.
(() => {
  const app = document.getElementById("recording-app");
  if (!app) return;
  const { t, errText, storedErr, api, el, dur, date } = DS(app);
  const ID = app.dataset.id;
  const $ = (id) => document.getElementById(id);
  const player = $("player");
  let speakers = {};
  let txDone = false;

  const badgeClass = (s) => (s === "done" ? "done" : s === "failed" ? "failed" : s === "none" ? "plain" : "running");
  const ts = (s) => dur(s);
  const colour = (label) => `spk-${(parseInt((label || "").replace(/\D/g, ""), 10) || 0) % 6}`;
  const who = (label) => (label ? speakers[label] || label : t("tx.unknown_speaker"));

  // --- transcript ------------------------------------------------------------------
  let txTimer = null;
  const loadTranscript = async () => {
    const res = await api("GET", `/api/recordings/${ID}/transcript`);
    if (!res.ok) return;
    const { recording: rec, transcript: tr } = res.data;
    $("rec-title").textContent = rec.title;
    document.title = document.title.replace(/^[^·]*/, `${rec.title} `);
    $("rec-meta").textContent = [
      date(rec.created_at),
      rec.duration_seconds != null ? dur(rec.duration_seconds) : null,
      rec.channels ? t("rec.channels", { n: rec.channels }) : null,
      tr.language ? t("tx.detected", { language: tr.language }) : null,
    ].filter(Boolean).join(" · ");

    const badge = $("tx-status");
    badge.textContent = t(`tx.status_${tr.status}`);
    badge.className = `badge ${badgeClass(tr.status)}`;
    const err = storedErr(tr.error);
    $("tx-error").hidden = !err;
    $("tx-error").textContent = err;
    if (tr.error && tr.error.detail) $("tx-error").append(el("span", { class: "mono detail" }, tr.error.detail));

    speakers = Object.fromEntries((tr.speakers || []).map((s) => [s.label, s.name]));
    renderSpeakers(tr.speakers || []);
    const segs = tr.segments || [];
    $("segments").replaceChildren(...segs.map((s) =>
      el("div", { class: "seg", "data-start": s.start ?? "", onclick: () => seek(s.start) },
        el("span", { class: "ts" }, ts(s.start)),
        el("strong", { class: colour(s.speaker) }, who(s.speaker)),
        el("span", { class: "text" }, s.text))));
    if (!segs.length) {
      const msg = tr.status === "queued" || tr.status === "running" ? t("tx.working") : tr.status === "failed" ? "" : t("tx.none");
      if (msg) $("segments").replaceChildren(el("p", { class: "empty" }, msg));
    }
    clearTimeout(txTimer);
    if (tr.status === "queued" || tr.status === "running") txTimer = setTimeout(loadTranscript, 2000);
    txDone = tr.status === "done";
    document.querySelectorAll("#insight-buttons button, #ask-form button, #ask-form input").forEach((b) => { b.disabled = !txDone; });
    if (!txDone && !$("insights").childElementCount) $("insights").replaceChildren(el("p", { class: "muted small" }, t("tx.insights_wait")));
  };
  const seek = (start) => { if (player && start != null) { player.currentTime = start; player.play().catch(() => {}); } };

  const renderSpeakers = (list) => {
    if (!list.length) { $("speakers").replaceChildren(); return; }
    const inputs = list.map((s) => el("label", {}, el("span", { class: colour(s.label) }, s.label),
      el("input", { "data-label": s.label, value: s.name || "", placeholder: t("tx.speaker_name") })));
    $("speakers").replaceChildren(...inputs, el("button", { class: "quiet", onclick: saveSpeakers }, t("tx.save_names")));
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
    if (!res.ok) { $("tx-error").hidden = false; $("tx-error").textContent = errText(res); return; }
    loadTranscript();
  });

  // --- insights ----------------------------------------------------------------------
  let insTimer = null;
  const loadInsights = async () => {
    const res = await api("GET", `/api/recordings/${ID}/insights`);
    if (!res.ok) return;
    if (res.data.length) $("insights").replaceChildren(...res.data.map(renderInsight));
    else if (txDone) $("insights").replaceChildren(el("p", { class: "muted small" }, t("tx.insights_empty")));
    clearTimeout(insTimer);
    if (res.data.some((i) => i.status === "queued" || i.status === "running")) insTimer = setTimeout(loadInsights, 1500);
    else if (res.data.some((i) => i.kind === "subject")) loadTranscript(); // title may have changed
  };
  const renderInsight = (i) => {
    const head = el("div", { class: "head" },
      el("span", { class: "kind" }, t(`tx.kind_${i.kind}`)),
      i.status !== "done" ? el("span", { class: `badge ${badgeClass(i.status)}` }, t(`tx.status_${i.status}`)) : null);
    const body = el("div", { class: "answer" });
    if (i.kind === "question" && i.question) body.append(el("p", { class: "q" }, i.question));
    if (i.status === "failed") {
      body.append(el("p", { class: "error" }, storedErr(i.error),
        i.error && i.error.detail ? el("span", { class: "mono detail" }, i.error.detail) : null));
    } else if (i.kind === "speakers" && i.data?.suggestions) {
      const sug = i.data.suggestions;
      body.append(sug.length
        ? el("ul", {}, ...sug.map((s) => el("li", {}, el("strong", { class: colour(s.label) }, s.name), ` · ${s.label}`,
          s.evidence ? el("div", { class: "muted small" }, s.evidence) : null)))
        : el("p", { class: "muted" }, t("tx.no_names")));
      if (sug.length) body.append(el("button", { onclick: async () => { await api("POST", `/api/insights/${i.id}/apply-speakers`); loadTranscript(); } }, t("tx.apply_names")));
    } else if (i.answer) body.append(el("div", { class: "answer-text" }, i.answer));
    const foot = el("div", { class: "foot" },
      el("span", {}, date(i.created_at)), i.model ? el("span", {}, i.model) : null,
      el("button", { class: "link", onclick: () => ask(i.kind, i.question) }, t("tx.rerun")),
      el("button", { class: "link", onclick: async () => { await api("DELETE", `/api/insights/${i.id}`); loadInsights(); } }, t("rec.delete")));
    return el("article", { class: "insight", "data-kind": i.kind, "data-status": i.status }, head, body, foot);
  };
  const ask = async (kind, question = null) => {
    const res = await api("POST", `/api/recordings/${ID}/insights`, { kind, question });
    $("insight-msg").hidden = res.ok;
    $("insight-msg").textContent = res.ok ? "" : errText(res);
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

  loadTranscript().then(loadInsights);
})();
