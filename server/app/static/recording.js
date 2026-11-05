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
  let tx = null;          // the last transcript loaded
  let editing = false;    // correcting the transcript by hand

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
    tx = tr;
    const segs = tr.segments || [];
    if (!editing) renderSegments();
    $("tx-edit").hidden = tr.status !== "done" || !segs.length || editing;
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
  const editedNote = (s) => s.edited
    ? el("span", { class: "edited", title: t("tx.edited_was", { text: s.original_text ?? s.text, speaker: who(s.original_speaker ?? s.speaker) }) }, t("tx.edited"))
    : null;
  const renderSegments = () => {
    const segs = tx?.segments || [];
    $("segments").classList.toggle("editing", editing);
    $("segments").replaceChildren(...segs.map((s) => {
      if (!editing) {
        return el("div", { class: "seg", "data-start": s.start ?? "", "data-idx": s.idx, onclick: () => seek(s.start) },
          el("span", { class: "ts" }, ts(s.start)),
          el("strong", { class: colour(s.speaker) }, who(s.speaker), editedNote(s)),
          el("span", { class: "text" }, s.text));
      }
      // Edit mode: speaker picker and text box per line; nothing seeks.
      const labels = (tx.speakers || []).map((x) => x.label);
      const pick = el("select", { "data-idx": s.idx, class: "seg-speaker", "aria-label": t("tx.speaker") },
        el("option", { value: "" }, t("tx.unknown_speaker")),
        ...labels.map((l) => el("option", { value: l, selected: l === s.speaker }, who(l))));
      const box = el("textarea", { "data-idx": s.idx, class: "seg-text", rows: 1, "aria-label": t("tx.line_text", { ts: ts(s.start) }) });
      box.value = s.text;
      const fit = () => { box.style.height = "auto"; box.style.height = `${box.scrollHeight}px`; };
      box.addEventListener("input", fit);
      requestAnimationFrame(fit);
      const revert = s.edited ? el("button", { class: "link small", type: "button", onclick: () => saveEdits([{ idx: s.idx, revert: true }]) }, t("tx.revert")) : null;
      return el("div", { class: "seg editing", "data-start": s.start ?? "", "data-idx": s.idx },
        el("span", { class: "ts" }, ts(s.start)),
        el("div", { class: "seg-edit" }, el("div", { class: "row" }, pick, revert), box));
    }));
  };
  const setEditing = (on) => {
    editing = on;
    $("edit-bar").hidden = !on;
    $("tx-edit").hidden = on;
    renderSegments();
    if (on) $("segments").querySelector("textarea")?.focus();
  };
  const collectEdits = () => (tx?.segments || []).flatMap((s) => {
    const text = $("segments").querySelector(`textarea[data-idx="${s.idx}"]`)?.value;
    const speaker = $("segments").querySelector(`select[data-idx="${s.idx}"]`)?.value;
    const e = { idx: s.idx };
    if (text != null && text.trim() !== s.text) e.text = text;
    if (speaker != null && (speaker || null) !== (s.speaker || null)) e.speaker = speaker;
    return Object.keys(e).length > 1 ? [e] : [];
  });
  const saveEdits = async (edits) => {
    if (!edits.length) { setEditing(false); return; }
    const res = await api("PATCH", `/api/recordings/${ID}/segments`, { edits });
    if (!res.ok) { $("edit-msg").hidden = false; $("edit-msg").textContent = errText(res); return; }
    const { changed_words: n, total_words: total } = res.data;
    $("edit-msg").hidden = false;
    $("edit-msg").textContent = n
      ? t("tx.edit_saved", { n, total, pct: Math.max(1, Math.round((n / Math.max(total, 1)) * 100)) })
      : t("tx.edit_nothing");
    tx = res.data.transcript;
    setEditing(false);
    loadInsights();
  };
  $("tx-edit").addEventListener("click", () => { $("edit-msg").hidden = true; setEditing(true); });
  $("edit-cancel").addEventListener("click", () => setEditing(false));
  $("edit-save").addEventListener("click", () => saveEdits(collectEdits()));

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
    if (tx?.edited && !confirm(t("tx.confirm_retranscribe"))) return;
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
    // Insights made from an older version of the transcript (edited or redone since).
    outdated = res.data.filter(isOutdated);
    $("outdated-note").hidden = !outdated.length;
    $("outdated-text").textContent = t("tx.outdated_n", { n: outdated.length });
    clearTimeout(insTimer);
    if (res.data.some((i) => i.status === "queued" || i.status === "running")) insTimer = setTimeout(loadInsights, 1500);
    else if (res.data.some((i) => i.kind === "subject")) loadTranscript(); // title may have changed
  };
  let outdated = [];
  const isOutdated = (i) => i.status === "done" && i.transcript_rev != null && tx && i.transcript_rev < tx.rev;
  $("outdated-rerun").addEventListener("click", async () => {
    // Replace each: queue it again on the corrected text, then drop the old answer.
    for (const i of outdated) {
      const res = await api("POST", `/api/recordings/${ID}/insights`, { kind: i.kind, question: i.question });
      if (res.ok) await api("DELETE", `/api/insights/${i.id}`);
    }
    loadInsights();
  });
  const renderInsight = (i) => {
    const head = el("div", { class: "head" },
      el("span", { class: "kind" }, t(`tx.kind_${i.kind}`)),
      i.status !== "done" ? el("span", { class: `badge ${badgeClass(i.status)}` }, t(`tx.status_${i.status}`))
        : isOutdated(i) ? el("span", { class: "badge queued", title: t("tx.outdated_hint") }, t("tx.outdated")) : null);
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
    return el("article", { class: "insight", "data-kind": i.kind, "data-status": i.status, "data-outdated": isOutdated(i) ? "1" : null }, head, body, foot);
  };
  const ask = async (kind, question = null) => {
    const res = await api("POST", `/api/recordings/${ID}/insights`, { kind, question });
    $("insight-msg").hidden = res.ok;
    $("insight-msg").textContent = res.ok ? "" : errText(res);
    loadInsights();
  };
  document.querySelectorAll("#insight-buttons button").forEach((b) => b.addEventListener("click", () => ask(b.dataset.kind)));
  // Ask: starts a chat about this meeting (follow-ups continue on the Ask page).
  $("ask-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = ev.target.question.value.trim();
    if (!q) return;
    const res = await api("POST", "/api/chats", { recording_ids: [ID], question: q });
    if (!res.ok) { $("insight-msg").hidden = false; $("insight-msg").textContent = errText(res); return; }
    location.href = res.data.page_url;
  });

  if (player) player.addEventListener("timeupdate", () => {
    const now = player.currentTime;
    document.querySelectorAll(".seg").forEach((s) => {
      const start = parseFloat(s.dataset.start), next = s.nextElementSibling ? parseFloat(s.nextElementSibling.dataset.start) : Infinity;
      s.classList.toggle("playing", now >= start && now < next);
    });
  });

  // Phones: Transcript / Insights tabs.
  const work = $("work");
  document.querySelectorAll(".view-tabs [data-view]").forEach((b) => b.addEventListener("click", () => {
    work.dataset.view = b.dataset.view;
    document.querySelectorAll(".view-tabs [data-view]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  }));

  loadTranscript().then(loadInsights);
})();
