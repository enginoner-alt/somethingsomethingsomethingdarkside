// Page shell: the phone menu drawer, and "panes" (long pages shown one section at a time).
(() => {
  const html = document.documentElement;
  const btn = document.getElementById("menu-btn");
  const scrim = document.getElementById("scrim");
  const nav = document.getElementById("sidenav");

  // --- phone drawer ------------------------------------------------------------------
  const setOpen = (open) => {
    html.classList.toggle("nav-open", open);
    btn?.setAttribute("aria-expanded", String(open));
    if (scrim) scrim.hidden = !open;
    if (open) nav?.querySelector(".navlist a")?.focus();
  };
  btn?.addEventListener("click", () => setOpen(!html.classList.contains("nav-open")));
  scrim?.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && html.classList.contains("nav-open")) { setOpen(false); btn?.focus(); }
  });

  // --- panes -------------------------------------------------------------------------
  // <section class="pane" id="pane-x" data-pane="x"> blocks; links with data-pane-link="x"
  // (in the side navigation). Without JavaScript every pane is simply shown.
  const panes = [...document.querySelectorAll("[data-pane]")];
  if (!panes.length) return;
  const links = [...document.querySelectorAll("[data-pane-link]")];
  const root = document.querySelector("[data-panes]");
  const storeKey = `pane:${location.pathname}`;
  const store = {
    get: () => { try { return sessionStorage.getItem(storeKey); } catch { return null; } },
    set: (v) => { try { sessionStorage.setItem(storeKey, v); } catch { /* private mode */ } },
  };

  const show = (name, { focus = false } = {}) => {
    if (!panes.some((p) => p.dataset.pane === name)) name = panes[0].dataset.pane;
    for (const p of panes) p.hidden = p.dataset.pane !== name;
    for (const a of links) {
      const on = a.dataset.paneLink === name;
      a.classList.toggle("active", on);
      if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
    }
    // A Save bar belongs to its form: hide it on panes outside that form.
    for (const bar of document.querySelectorAll("[data-form-actions]")) {
      const current = panes.find((p) => p.dataset.pane === name);
      bar.hidden = !bar.closest("form")?.contains(current);
    }
    store.set(name);
    history.replaceState(null, "", `#${name}`);
    if (focus) panes.find((p) => p.dataset.pane === name)?.querySelector("h2")?.focus();
  };

  html.classList.add("panes-on");
  // A result the server wants seen (e.g. a Verify result) wins, then the address, then the last pane used.
  const initial = root?.dataset.initialPane || location.hash.slice(1) || store.get();
  show(initial || panes[0].dataset.pane);

  for (const a of links) {
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      show(a.dataset.paneLink, { focus: true });
      setOpen(false);
      window.scrollTo(0, 0);
    });
  }
  // A field the browser refuses on submit may sit in a hidden pane: show that pane.
  document.addEventListener("invalid", (ev) => {
    const pane = ev.target.closest?.("[data-pane]");
    if (pane && pane.hidden) show(pane.dataset.pane);
  }, true);
})();
