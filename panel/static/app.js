// Progressive enhancement only: every page works without this file.
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Copy buttons: data-copy names the element whose text to copy.
document.querySelectorAll("[data-copy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const el = document.getElementById(btn.dataset.copy);
    try {
      await navigator.clipboard.writeText(el.textContent.trim());
      btn.textContent = "Copied";
    } catch (_) {
      const r = document.createRange(); r.selectNodeContents(el);
      const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
      btn.textContent = "Press Ctrl+C";
    }
    setTimeout(() => { btn.textContent = "Copy"; }, 2000);
  });
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

// Update button. Everything from the server is shown with textContent, never as HTML.
(() => {
  const open = document.getElementById("update-open");
  const dlg = document.getElementById("update-dialog");
  if (!open || !dlg || typeof dlg.showModal !== "function") return;
  const $ = (id) => document.getElementById(id);
  const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const infoUrl = open.dataset.url, startUrl = dlg.dataset.startUrl;
  let timer = null, requestedAt = 0, failures = 0, sawStart = false;

  const ago = (t) => {
    if (!t) return "never";
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " minutes ago" : s < 129600 ? Math.round(s / 3600) + " hours ago" : Math.round(s / 86400) + " days ago";
  };
  const when = (d) => (d.status.started || 0) >= requestedAt - 2;  // this status belongs to the run we asked for

  function render(d) {
    const v = d.version || {};
    $("update-version").textContent = v.short ? v.short + "  " + (v.date || "") : "unknown";
    const a = d.available || {};
    $("update-available").textContent = a.error ? "Could not check: " + a.error
      : a.behind === null || a.behind === undefined ? "Not checked yet"
      : a.behind === 0 ? "Up to date (checked " + ago(a.checked) + ")"
      : a.behind + " new commit" + (a.behind === 1 ? "" : "s") + " available (checked " + ago(a.checked) + ")";
    const st = d.status || {};
    const waiting = requestedAt && !when(d) && st.state !== "requested";
    let text = st.message || "No update has run yet.";
    if (waiting || st.state === "requested") text = "Waiting for the updater to start ...";
    else if (st.state === "running") text = st.step || "Running ...";
    else if (st.state === "stale") text = "The last update stopped without finishing. Check the log.";
    else if (st.state === "failed") text = "Failed: " + (st.message || "see the log") + (st.rolled_back ? " (rolled back)" : "");
    $("update-status").textContent = text;
    const log = $("update-log");
    log.hidden = !d.log;
    log.textContent = d.log || "";
    log.scrollTop = log.scrollHeight;
    $("update-reason").textContent = d.can_start ? "" : d.reason || "";
    $("update-go").disabled = !d.can_start;
    $("update-dot").hidden = !(a.behind > 0);
    return waiting || st.state === "running" || st.state === "requested";
  }

  async function load() {
    clearTimeout(timer);
    try {
      const r = await fetch(infoUrl, { credentials: "same-origin" });
      if (!r.ok) throw new Error(r.status);
      failures = 0;
      const d = await r.json();
      const st = d.status || {};
      if (st.state === "running" && when(d)) sawStart = true;
      const busy = render(d);
      if (busy) timer = setTimeout(load, 1500);
      else if (requestedAt && sawStart && st.state === "success") { $("update-status").textContent += " Reloading ..."; setTimeout(() => location.reload(), 1500); }
    } catch (_) {
      failures += 1;
      // The panel restarts in the middle of an update, so a few failed requests are expected.
      if (requestedAt && failures < 200) { $("update-status").textContent = "The panel is restarting ..."; timer = setTimeout(load, 2000); }
      else $("update-status").textContent = "Could not reach the panel.";
    }
  }

  open.addEventListener("click", () => { requestedAt = 0; sawStart = false; dlg.showModal(); load(); });
  $("update-close").addEventListener("click", () => { clearTimeout(timer); dlg.close(); });
  dlg.addEventListener("close", () => clearTimeout(timer));
  $("update-go").addEventListener("click", async () => {
    if (!window.confirm("Update now? The panel may restart and be unavailable for a few seconds.")) return;
    $("update-go").disabled = true;
    try {
      const r = await fetch(startUrl, { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf } });
      const d = await r.json();
      if (!d.ok) { $("update-status").textContent = d.error || "Could not start the update."; return load(); }
      requestedAt = d.requested_at; sawStart = false; failures = 0;
      $("update-status").textContent = "Waiting for the updater to start ...";
      load();
    } catch (_) { $("update-status").textContent = "Could not send the request."; }
  });

  // A dot on the button when GitHub has something new. One quiet request per page view, admins only.
  fetch(infoUrl, { credentials: "same-origin" }).then((r) => r.ok ? r.json() : null).then((d) => {
    if (d && d.available && d.available.behind > 0) $("update-dot").hidden = false;
  }).catch(() => {});
})();
