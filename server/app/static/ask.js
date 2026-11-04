// Ask: pick meetings (within the admin's context limit), then a chat with follow-ups.
(() => {
  const root = document.getElementById("ask-app");
  if (!root) return;
  const { t, errText, storedErr, api, el, dur, date } = DS(root);
  const $ = (id) => document.getElementById(id);
  const CHAT = root.dataset.chatId;
  const showError = (text) => { const e = $("ask-error"); e.hidden = !text; e.textContent = text || ""; };

  // --- the list of chats ------------------------------------------------------------
  const loadList = async () => {
    const res = await api("GET", "/api/chats");
    if (!res.ok) return;
    $("chat-list").replaceChildren(...(res.data.length ? res.data.map((c) =>
      el("a", { href: c.page_url, class: String(c.id) === CHAT ? "active" : null, "aria-current": String(c.id) === CHAT ? "page" : null },
        el("span", { class: "chat-title" }, c.title),
        el("span", { class: "small muted" }, `${date(c.updated_at)} · ${t("ask.meetings_n", { n: c.meetings.length })}`)))
      : [el("p", { class: "muted small" }, t("ask.no_chats"))]));
  };

  // --- new chat: pick meetings --------------------------------------------------------
  if (!CHAT) {
    const picked = new Set(root.dataset.with.split(",").filter(Boolean));
    let meetings = [], limit = 0;
    const q = $("new-q"), btn = $("new-chat-btn");

    const used = () => meetings.filter((m) => picked.has(m.id)).reduce((n, m) => n + m.chars, 0) + q.value.length;
    const update = () => {
      const u = used(), pct = limit ? Math.round((u / limit) * 100) : 0, over = u > limit;
      $("budget-fill").style.width = `${Math.min(100, pct)}%`;
      $("budget").classList.toggle("over", over);
      $("budget-text").textContent = over ? t("ask.budget_over", { pct }) : t("ask.budget", { pct, n: picked.size });
      btn.disabled = over || !picked.size || !q.value.trim();
    };
    const draw = () => {
      const s = $("source-search").value.trim().toLocaleLowerCase();
      const shown = meetings.filter((m) => !s || m.title.toLocaleLowerCase().includes(s) || picked.has(m.id));
      $("source-list").replaceChildren(...(meetings.length ? shown.map((m) => {
        const share = limit ? Math.max(1, Math.round((m.chars / limit) * 100)) : 0;
        const box = el("input", { type: "checkbox", value: m.id, checked: picked.has(m.id) });
        box.addEventListener("change", () => { box.checked ? picked.add(m.id) : picked.delete(m.id); update(); });
        return el("label", { class: "source" }, box,
          el("span", { class: "grow" }, el("span", { class: "source-title" }, m.title),
            el("span", { class: "small muted" }, [date(m.created_at), m.duration_seconds != null ? dur(m.duration_seconds) : null].filter(Boolean).join(" · "))),
          el("span", { class: "small muted share" }, t("ask.share", { pct: share })));
      }) : [el("p", { class: "muted small" }, t("ask.no_sources"))]));
      update();
    };
    (async () => {
      const res = await api("GET", "/api/chats/sources");
      if (!res.ok) { showError(errText(res)); return; }
      ({ meetings, limit } = res.data);
      for (const id of [...picked]) if (!meetings.some((m) => m.id === id)) picked.delete(id);
      draw();
    })();
    $("source-search").addEventListener("input", draw);
    q.addEventListener("input", update);
    $("new-chat").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      btn.disabled = true;
      const res = await api("POST", "/api/chats", { recording_ids: [...picked], question: q.value.trim() });
      if (!res.ok) { showError(errText(res)); update(); return; }
      location.href = res.data.page_url;
    });
    loadList();
    return;
  }

  // --- a chat ------------------------------------------------------------------------
  let timer = null;
  const render = (chat) => {
    $("thread-meetings").replaceChildren(
      t("ask.about"), " ",
      ...chat.meetings.flatMap((m, i) => [i ? ", " : "", el("a", { href: m.page_url }, `${i + 1}. ${m.title}`)]));
    document.title = document.title.replace(/^[^·]*/, `${chat.title} `);
    $("messages").replaceChildren(...chat.messages.map((m) => {
      const body = m.role === "user" ? el("div", { class: "bubble" }, m.content)
        : m.status === "failed" ? el("p", { class: "error" }, storedErr(m.error), m.error?.detail ? el("span", { class: "mono detail" }, m.error.detail) : null)
        : m.status === "done" ? el("div", { class: "answer-text" }, m.content)
        : el("span", { class: "badge running" }, t("ask.thinking"));
      return el("article", { class: `msg ${m.role}`, "data-status": m.status }, body,
        m.role === "assistant" && m.status === "done" ? el("div", { class: "small muted foot" }, [date(m.created_at), m.model].filter(Boolean).join(" · ")) : null);
    }));
    const box = $("messages");
    box.scrollTop = box.scrollHeight;
    const busy = chat.messages.some((m) => m.status === "queued" || m.status === "running");
    $("followup").querySelector("button").disabled = busy;
    clearTimeout(timer);
    if (busy) timer = setTimeout(load, 1500);
  };
  const load = async () => {
    const res = await api("GET", `/api/chats/${CHAT}`);
    if (res.ok) render(res.data); else showError(errText(res));
  };
  $("followup").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = $("followup-q");
    if (!q.value.trim()) return;
    const res = await api("POST", `/api/chats/${CHAT}/messages`, { question: q.value.trim() });
    if (!res.ok) { showError(errText(res)); return; }
    showError(""); q.value = ""; render(res.data);
  });
  // Enter sends, Shift+Enter makes a new line.
  $("followup-q").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("followup").requestSubmit(); }
  });
  $("chat-delete").addEventListener("click", async () => {
    if (!confirm(t("ask.confirm_delete"))) return;
    await api("DELETE", `/api/chats/${CHAT}`);
    location.href = "/ask";
  });
  load();
  loadList();
})();
