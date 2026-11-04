// Refresh the job table while anything is queued or running.
(() => {
  const tbody = document.getElementById("job-rows");
  if (!tbody) return;
  const labels = JSON.parse(tbody.dataset.statusLabels || "{}");

  const cell = (text, cls) => {
    const td = document.createElement("td");
    if (cls) td.className = cls;
    td.textContent = text;
    return td;
  };

  const draw = (jobs) => {
    if (!jobs.length) return;
    tbody.replaceChildren(
      ...jobs.map((j) => {
        const tr = document.createElement("tr");
        const status = document.createElement("td");
        const badge = document.createElement("span");
        badge.className = `badge ${j.status}`;
        badge.textContent = labels[j.status] || j.status;
        status.append(badge);
        const detail = j.error ? j.error.trim().split("\n").pop() : "";
        const finished = j.finished_at ? j.finished_at.slice(0, 16).replace("T", " ") : "—";
        tr.append(cell(j.id), cell(j.queue), cell(j.type), status, cell(finished, "muted"), cell(detail, "mono"));
        return tr;
      }),
    );
  };

  const tick = async () => {
    try {
      const res = await fetch("/admin/jobs.json", { credentials: "same-origin" });
      if (!res.ok) return;
      const jobs = await res.json();
      draw(jobs);
      if (jobs.some((j) => j.status === "queued" || j.status === "running")) {
        setTimeout(tick, 1500);
      }
    } catch {
      setTimeout(tick, 5000);
    }
  };
  tick();
})();
