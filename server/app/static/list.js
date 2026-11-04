// Recordings list (home page): search, open, download, delete, upload a file.
(() => {
  const root = document.getElementById("list-app");
  if (!root) return;
  const { t, errText, api, el, dur, date } = DS(root);
  const $ = (id) => document.getElementById(id);
  const list = $("rec-list"), search = $("rec-search");
  let rows = [];

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
    return el("div", { class: "rec-row", "data-id": r.id }, titleEl, side, meta);
  };

  const draw = () => {
    const q = search.value.trim().toLocaleLowerCase();
    const shown = q ? rows.filter((r) => r.title.toLocaleLowerCase().includes(q)) : rows;
    if (!rows.length) {
      list.replaceChildren(el("div", { class: "empty" },
        el("h2", {}, t("rec.empty")), el("p", {}, t("rec.empty_hint")),
        el("a", { class: "button primary", href: "/record" }, t("rec.new"))));
      return;
    }
    list.replaceChildren(...(shown.length ? shown.map(row) : [el("p", { class: "empty" }, t("rec.no_match"))]));
  };

  const refresh = async () => {
    const res = await api("GET", "/api/recordings");
    if (!res.ok) { list.replaceChildren(el("p", { class: "error" }, errText(res))); return; }
    rows = res.data;
    draw();
    if (rows.some((r) => r.status === "recording")) setTimeout(refresh, 5000);
  };

  const remove = async (id) => {
    if (!confirm(t("rec.confirm_delete"))) return;
    await api("DELETE", `/api/recordings/${id}`);
    refresh();
  };

  search.addEventListener("input", draw);

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
