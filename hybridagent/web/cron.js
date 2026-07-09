/* Schedule rail — list / pause / resume / delete cron jobs. Served from /web/cron.js. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function when(ts) {
    if (!ts) return "—";
    try {
      var d = new Date(Number(ts) * 1000);
      return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return "—";
    }
  }

  async function api(path, opts) {
    var r = await fetch(path, opts);
    return r.json().catch(function () { return {}; });
  }

  function paint(jobs) {
    var el = document.getElementById("cron-list");
    if (!el) return;
    jobs = jobs || [];
    if (!jobs.length) {
      el.innerHTML = '<div class="empty">No schedules. Add one below or: <code>praxis jobs run schedule</code></div>';
      if (window.PraxisShell) window.PraxisShell.signal("ops", false);
      return;
    }
    el.innerHTML = "";
    var anyOn = false;
    jobs.forEach(function (j) {
      if (j.enabled) anyOn = true;
      var row = document.createElement("div");
      row.className = "cron-job" + (j.enabled ? "" : " off");
      row.innerHTML =
        '<div class="cj-head">' +
        '<span class="cj-id">' + esc(j.job_id) + "</span>" +
        '<span class="cj-state">' + (j.enabled ? "on" : "paused") + "</span>" +
        "</div>" +
        '<div class="cj-goal"></div>' +
        '<div class="cj-meta">' + esc(j.schedule || "") +
        " · next " + esc(when(j.next_run_ts)) +
        " · " + esc(j.mode || "do") +
        (j.runs != null ? " · runs " + esc(String(j.runs)) : "") +
        "</div>" +
        '<div class="cj-actions">' +
        '<button type="button" class="ghost cj-tog">' + (j.enabled ? "Pause" : "Resume") + "</button>" +
        '<button type="button" class="ghost cj-del">Delete</button>' +
        "</div>";
      row.querySelector(".cj-goal").textContent = j.goal || "";
      row.querySelector(".cj-tog").onclick = function () {
        toggle(j.job_id, !j.enabled);
      };
      row.querySelector(".cj-del").onclick = function () {
        if (confirm("Delete schedule " + j.job_id + "?")) del(j.job_id);
      };
      el.appendChild(row);
    });
    if (window.PraxisShell) window.PraxisShell.signal("ops", anyOn || !!(document.querySelectorAll("#tasks .task").length));
  }

  async function refresh() {
    try {
      var res = await api("/api/cron");
      paint(res.jobs || []);
    } catch (_) {
      var el = document.getElementById("cron-list");
      if (el) el.innerHTML = '<div class="empty">Could not load schedules.</div>';
    }
  }

  async function toggle(id, enabled) {
    await api("/api/cron/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: id, enabled: enabled })
    });
    refresh();
  }

  async function del(id) {
    await api("/api/cron/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: id })
    });
    refresh();
  }

  async function add(ev) {
    if (ev) ev.preventDefault();
    var goal = (document.getElementById("cronGoal") || {}).value || "";
    var schedule = (document.getElementById("cronSched") || {}).value || "";
    goal = goal.trim();
    schedule = schedule.trim();
    if (!goal || !schedule) return;
    var res = await api("/api/cron", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: goal, schedule: schedule, mode: "do", name: "colleague" })
    });
    if (res.error) {
      if (window.PraxisToast) window.PraxisToast(res.error, "error");
      else alert(res.error);
      return;
    }
    var g = document.getElementById("cronGoal");
    if (g) g.value = "";
    refresh();
    if (window.PraxisToast) window.PraxisToast("Scheduled " + (res.job_id || ""), "ok");
  }

  window.PraxisCron = { refresh: refresh, add: add };

  function boot() {
    var form = document.getElementById("cronForm");
    if (form) form.addEventListener("submit", add);
    refresh();
    setInterval(refresh, 8000);
    if (window.PraxisBus && window.PraxisBus.on) {
      window.PraxisBus.on("cron", function () { refresh(); });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
