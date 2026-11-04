// Recordings list (home page): search, open, download, delete, upload a file.
(() => {
  const root = document.getElementById("list-app");
  if (!root) return;
  const { t, errText, api, el, dur, date } = DS(root);
  const $ = (id) => document.getElementById(id);
  const list = $("rec-list"), search = $("rec-search"), pager = $("rec-pager");
  const PAGE = 25;
  let rows = [], total = 0, offset = 0, timer = null;
  const picked = new Set();  // meetings chosen for Ask
  const askBtn = $("ask-selected");
  const updateAsk = () => {
    askBtn.hidden = !picked.size;
    askBtn.textContent = t("rec.ask_selected", { n: picked.size });
    askBtn.href = `/ask?with=${[...picked].join(",")}`;
  };

  const STATE_BADGE = { uploaded: "done", recording: "running", interrupted: "queued", failed: "failed" };

  const row = (r) => {
    const meta = el("div", { class: "meta" },
      el("span", {}, date(r.created_at)),
      r.duration_seconds != null ? el("span", {}, dur(r.duration_seconds)) : null,
      r.channels ? el("span", {}, t("rec.channels", { n: r.channels })) : null,
      r.source === "upload" ? el("span", {}, t("rec.uploaded_file")) : null);
    const titleEl = r.audio_url
      ? el("a", { class: "title", href: r.page_url }, r.title)
      : el("span", { class: "title" }, r.title);
    const side = el("div", { class: "side" },
      r.status !== "uploaded" ? el("span", { class: `badge ${STATE_BADGE[r.status] || "plain"}` }, t(`rec.state_${r.status}`)) : null,
      r.audio_url ? el("a", { class: "button quiet", href: r.audio_url, download: true }, t("rec.download")) : null,
      r.status !== "recording" ? el("button", { class: "quiet", onclick: () => remove(r.id) }, t("rec.delete")) : null);
    const box = r.audio_url ? el("input", { type: "checkbox", class: "pick", "aria-label": t("rec.pick_for_ask"), checked: picked.has(r.id) }) : el("span");
    box.addEventListener?.("change", () => { box.checked ? picked.add(r.id) : picked.delete(r.id); updateAsk(); });
    return el("div", { class: "rec-row with-pick", "data-id": r.id }, box, titleEl, side, meta);
  };

  const draw = () => {
    const q = search.value.trim();
    if (!rows.length && !q && !offset) {
      list.replaceChildren(el("div", { class: "empty" },
        el("h2", {}, t("rec.empty")), el("p", {}, t("rec.empty_hint")),
        el("a", { class: "button primary", href: "/record" }, t("rec.new"))));
    } else {
      list.replaceChildren(...(rows.length ? rows.map(row) : [el("p", { class: "empty" }, t("rec.no_match"))]));
    }
    // Pages of 25 instead of one endless list.
    const pages = Math.max(1, Math.ceil(total / PAGE)), page = Math.floor(offset / PAGE) + 1;
    pager.replaceChildren(...(pages > 1 ? [
      page > 1 ? el("button", { class: "quiet", onclick: () => go(offset - PAGE) }, `← ${t("common.newer")}`) : el("span"),
      el("span", { class: "muted small" }, t("common.page_of", { page, pages })),
      page < pages ? el("button", { class: "quiet", onclick: () => go(offset + PAGE) }, `${t("common.older")} →`) : el("span"),
    ] : []));
  };
  const go = (o) => { offset = Math.max(0, o); refresh(); window.scrollTo(0, 0); };

  let poll = null;
  const refresh = async () => {
    const q = encodeURIComponent(search.value.trim());
    const res = await api("GET", `/api/recordings?limit=${PAGE}&offset=${offset}&q=${q}`);
    if (!res.ok) { list.replaceChildren(el("p", { class: "error" }, errText(res))); return; }
    rows = res.data;
    total = Number(res.headers?.get("X-Total-Count") ?? rows.length);
    draw();
    clearTimeout(poll);
    if (rows.some((r) => r.status === "recording")) poll = setTimeout(refresh, 5000);
  };

  const remove = async (id) => {
    if (!confirm(t("rec.confirm_delete"))) return;
    await api("DELETE", `/api/recordings/${id}`);
    refresh();
  };

  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { offset = 0; refresh(); }, 250); });

  const form = $("upload-form"), file = $("upload-file"), status = $("upload-status"), consent = $("consent");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!consent.checked) { status.textContent = t("rec.err_consent"); consent.focus(); return; }
    if (!file.files[0]) return;
    const fd = new FormData();
    fd.append("file", file.files[0]);
    fd.append("consent", "true");
    status.textContent = t("rec.uploading");
    const res = await api("POST", "/api/recordings/upload", fd);
    status.textContent = res.ok ? t("rec.upload_done") : errText(res);
    if (res.ok) { form.reset(); refresh(); }
  });

  refresh();
})();
