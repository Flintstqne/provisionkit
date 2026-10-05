// Progressive enhancement only: every page works without this file.
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Click-to-sort tables.
document.querySelectorAll("table[data-sortable] th[data-sort]").forEach((th, idx) => {
  th.addEventListener("click", () => {
    const tbody = th.closest("table").tBodies[0];
    const dir = th.dataset.dir === "asc" ? -1 : 1;
    th.closest("tr").querySelectorAll("th").forEach((h) => delete h.dataset.dir);
    th.dataset.dir = dir === 1 ? "asc" : "desc";
    const col = th.cellIndex;
    const val = (tr) => tr.cells[col].dataset.v ?? tr.cells[col].textContent.trim();
    const rows = [...tbody.rows].sort((a, b) => {
      const x = val(a), y = val(b);
      const nx = parseFloat(x), ny = parseFloat(y);
      return (isNaN(nx) || isNaN(ny) ? x.localeCompare(y) : nx - ny) * dir;
    });
    rows.forEach((r) => tbody.appendChild(r));
  });
});

// Live job log.
const logEl = document.getElementById("job-log");
if (logEl) {
  const statusEl = document.getElementById("job-status");
  const poll = async () => {
    try {
      const r = await fetch(logEl.dataset.url, { credentials: "same-origin" });
      if (!r.ok) return;
      const d = await r.json();
      const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
      logEl.textContent = d.log || "(waiting for output)";
      if (atBottom) logEl.scrollTop = logEl.scrollHeight;
      if (d.status === "queued" || d.status === "running") setTimeout(poll, 1500);
      else if (statusEl && statusEl.dataset.live) window.location.reload();
    } catch (_) { setTimeout(poll, 4000); }
  };
  poll();
}
