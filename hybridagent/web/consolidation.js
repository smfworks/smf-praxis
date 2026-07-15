/* Praxis Consolidation panel — the Mind-pane section that shows active
 * memory consolidation status (enabled, interval, pending count, next run)
 * and a manual "Run now" trigger. Served from /web/consolidation.js.
 * Polls /api/consolidation every 15s.
 */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  async function api(url, body) {
    var opt = body
      ? { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body) }
      : undefined;
    var r = await fetch(url, opt);
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }

  var mount = null, data = null, runBtn = null;

  function fmtTs(ts) {
    if (!ts || ts <= 0) return "\u2014";
    var d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  async function load() {
    try {
      data = await api("/api/consolidation");
      renderPanel();
    } catch (_) {
      if (window.PraxisPanelError && mount) window.PraxisPanelError(mount, "Consolidation", load);
      else if (mount) mount.innerHTML = '<div class="empty">Consolidation unavailable.</div>';
    }
  }

  function renderPanel() {
    if (!mount || !data) return;
    mount.innerHTML = "";
    var card = el("div", "cons-card");
    var enabled = !!data.enabled;
    card.appendChild(el("div", "cons-status " + (enabled ? "on" : "off"),
      (enabled ? "\u25cf Enabled" : "\u25cb Disabled")));
    var meta = el("div", "cons-meta");
    meta.innerHTML =
      '<span>interval ' + esc(data.intervalMinutes) + 'm</span>' +
      '<span>window ' + esc(data.windowSize) + '</span>' +
      '<span>min ' + esc(data.minItemsToConsolidate) + '</span>' +
      '<span>max conn ' + esc(data.maxConnections) + '</span>';
    card.appendChild(meta);
    var pend = el("div", "cons-row");
    pend.innerHTML = '<span class="cons-label">Pending</span>' +
      '<span class="cons-value">' + esc(data.pending) + ' unconsolidated</span>';
    card.appendChild(pend);
    var next = el("div", "cons-row");
    next.innerHTML = '<span class="cons-label">Next run</span>' +
      '<span class="cons-value">' + fmtTs(data.next_run_ts) + '</span>';
    card.appendChild(next);
    if (data.last_report) {
      var last = el("div", "cons-row cons-last");
      last.innerHTML = '<span class="cons-label">Last pass</span>' +
        '<span class="cons-value">' +
        (data.last_report.insights_written || 0) + ' insights, ' +
        (data.last_report.connections_made || 0) + ' connections, ' +
        (data.last_report.items_reviewed || 0) + ' reviewed</span>';
      card.appendChild(last);
    }
    var actions = el("div", "cons-actions");
    runBtn = el("button", "cons-run-btn" + (enabled ? "" : " disabled"),
      "Run now");
    runBtn.type = "button";
    runBtn.disabled = !enabled;
    runBtn.title = enabled
      ? "Trigger one consolidation pass now"
      : "Consolidation is disabled (enable via 'praxis consolidation enable')";
    runBtn.onclick = onRun;
    actions.appendChild(runBtn);
    card.appendChild(actions);
    mount.appendChild(card);
  }

  async function onRun() {
    if (!runBtn) return;
    runBtn.disabled = true;
    runBtn.textContent = "Running\u2026";
    try {
      var res = await api("/api/consolidation/run", {});
      if (res.error) {
        runBtn.textContent = "Run now";
        runBtn.disabled = false;
        if (window.PraxisPanelError && mount) {
          window.PraxisPanelError(mount, "Consolidation", load);
        }
      } else {
        runBtn.textContent = "Done";
        setTimeout(load, 800);
      }
    } catch (_) {
      runBtn.textContent = "Run now";
      runBtn.disabled = false;
    }
  }

  function boot() {
    mount = document.getElementById("consolidation-mount");
    if (!mount) return;
    load();
    setInterval(load, 15000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();