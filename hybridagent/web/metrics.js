/* Praxis Observability — eval pass-rate trend, governance decision mix (allow /
 * held / denied + injection & egress blocks), and run-status counts, all
 * aggregated from the durable tables. Push-refreshed. Served from /web/metrics.js.
 */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[c];
    });
  }
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  async function getJSON(url) {
    var r = await fetch(url);
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }
  function sum(obj) {
    var t = 0;
    Object.keys(obj || {}).forEach(function (k) { t += obj[k]; });
    return t;
  }
  function fmtUsd(n) { return "$" + (Number(n) || 0).toFixed(4); }
  function fmtTok(n) {
    n = Number(n) || 0;
    return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  }
  var RULE_LABEL = {
    autonomous_allow: "autonomous", allowlist_denied: "allowlist block",
    egress_blocked: "egress block", kill_switch_denied: "kill-switch",
    approval_required: "approval", dual_approval: "dual approval"
  };

  var mount = null, overlay = null, data = null;

  async function load() {
    try {
      data = await getJSON("/api/metrics");
      renderPanel();
      if (overlay && overlay.classList.contains("show")) renderOverlay();
    } catch (_) { window.PraxisPanelError(mount, "Metrics", load); }
  }

  function renderPanel() {
    if (!mount || !data) return;
    mount.innerHTML = "";
    var ev = data.evals || [];
    var last = ev[ev.length - 1];
    var bv = (data.decisions || {}).by_verdict || {};
    mount.appendChild(el("div", "mx-stat",
      "<b>Evals</b><span>" + (last ? last.passes + "/" + last.total : "\u2014") + "</span>"));
    mount.appendChild(el("div", "mx-stat",
      "<b>Decisions</b><span>" + ((data.decisions || {}).total || 0) + "</span>"));
    var rt0 = data.routing || {};
    mount.appendChild(el("div", "mx-stat",
      "<b>Spend</b><span>" + fmtUsd(rt0.total_cost_usd) + "</span>"));
    var mix = el("div", "mx-mini");
    mix.innerHTML =
      '<span class="ok">' + (bv.allow || 0) + ' allow</span>' +
      '<span class="warn">' + (bv.needs_approval || 0) + ' held</span>' +
      '<span class="bad">' + (bv.deny || 0) + ' denied</span>';
    mount.appendChild(mix);
    var open = el("button", "mx-open", "Open metrics \u2922");
    open.type = "button";
    open.onclick = openOverlay;
    mount.appendChild(open);
  }

  function ensureOverlay() {
    if (overlay) return;
    overlay = el("div", "mx-overlay");
    overlay.appendChild(el("div", "mx-box"));
    overlay.addEventListener("click", function (e) { if (e.target === overlay) closeOverlay(); });
    document.body.appendChild(overlay);
  }
  function openOverlay() { ensureOverlay(); overlay.classList.add("show"); renderOverlay(); }
  function closeOverlay() { if (overlay) overlay.classList.remove("show"); }

  function bars(obj, klass) {
    var total = sum(obj) || 1;
    var rows = el("div", "mx-rows");
    var keys = Object.keys(obj || {}).sort(function (a, b) { return obj[b] - obj[a]; });
    if (!keys.length) { rows.appendChild(el("div", "empty", "No data yet.")); return rows; }
    keys.forEach(function (k) {
      var pct = Math.round((obj[k] / total) * 100);
      var row = el("div", "mx-row");
      row.innerHTML =
        '<span class="lbl">' + esc(RULE_LABEL[k] || k) + '</span>' +
        '<span class="mx-track"><span class="mx-fill ' + (klass(k) || '') +
        '" style="width:' + pct + '%"></span></span>' +
        '<span class="num">' + obj[k] + '</span>';
      rows.appendChild(row);
    });
    return rows;
  }

  function costBars(models) {
    var rows = el("div", "mx-rows");
    if (!models || !models.length) {
      rows.appendChild(el("div", "empty", "No spend recorded yet."));
      return rows;
    }
    var max = models.reduce(function (m, x) { return Math.max(m, x.cost_usd); }, 0) || 1;
    models.forEach(function (x) {
      var pct = x.cost_usd > 0 ? Math.max(2, Math.round((x.cost_usd / max) * 100)) : 0;
      var allLocal = x.runs > 0 && x.local_runs >= x.runs;
      var row = el("div", "mx-row cost");
      row.innerHTML =
        '<span class="lbl" title="' + esc(x.model) + '">' + esc(x.model) + '</span>' +
        '<span class="mx-track"><span class="mx-fill ' + (allLocal ? 'ok' : '') +
        '" style="width:' + pct + '%"></span></span>' +
        '<span class="num">' + fmtUsd(x.cost_usd) + '</span>';
      rows.appendChild(row);
      rows.appendChild(el("div", "mx-sub",
        x.runs + ' run' + (x.runs === 1 ? '' : 's') + ' \u00b7 ' +
        fmtTok(x.tokens) + ' tok' + (allLocal ? ' \u00b7 local (free)' : '')));
    });
    return rows;
  }

  function spendTrend(trend) {
    if (!trend || !trend.length) return el("div", "empty", "No spend trend yet.");
    var wrap = el("div", "mx-trend");
    var max = trend.reduce(function (m, x) { return Math.max(m, x.cost_usd); }, 0) || 1;
    trend.forEach(function (x) {
      var bar = el("div", "mx-bar" + (x.cost_usd > 0 ? " cloud" : ""));
      bar.style.height = (x.cost_usd > 0 ? Math.max(6, Math.round((x.cost_usd / max) * 100)) : 4) + "%";
      bar.appendChild(el("span", null, fmtUsd(x.cost_usd)));
      bar.title = (x.goal || x.run_id) + " \u2014 " + fmtUsd(x.cost_usd) +
        (x.local ? " (local)" : "");
      wrap.appendChild(bar);
    });
    return wrap;
  }

  function renderOverlay() {
    var box = overlay.querySelector(".mx-box");
    box.innerHTML = "";
    var head = el("div", "mx-head");
    head.innerHTML = '<div><b>Observability</b></div>';
    var close = el("button", "mx-close", "\u2715");
    close.type = "button";
    close.onclick = closeOverlay;
    head.appendChild(close);
    box.appendChild(head);

    // Eval pass-rate trend
    box.appendChild(el("div", "mx-section", "Eval pass-rate (recent runs)"));
    var ev = data.evals || [];
    if (!ev.length) {
      box.appendChild(el("div", "empty", "No eval history yet \u2014 run `praxis eval --save`."));
    } else {
      var trend = el("div", "mx-trend");
      ev.forEach(function (r) {
        var ratio = r.total ? r.passes / r.total : 0;
        var bar = el("div", "mx-bar" + (ratio < 1 ? " miss" : ""));
        bar.style.height = Math.max(6, Math.round(ratio * 100)) + "%";
        bar.appendChild(el("span", null, r.passes + "/" + r.total));
        trend.appendChild(bar);
      });
      box.appendChild(trend);
    }

    // Governance decision mix
    box.appendChild(el("div", "mx-section", "Decisions by verdict"));
    box.appendChild(bars((data.decisions || {}).by_verdict, function (k) {
      if (k === "allow") return "ok";
      if (k === "deny") return "bad";
      if (k === "needs_approval") return "warn";
      return "";
    }));

    box.appendChild(el("div", "mx-section", "Decisions by policy rule"));
    box.appendChild(bars((data.decisions || {}).by_rule, function (k) {
      if (k === "autonomous_allow") return "ok";
      if (k.indexOf("denied") >= 0 || k === "egress_blocked") return "bad";
      return "warn";
    }));

    // Run status
    box.appendChild(el("div", "mx-section", "Runs by status"));
    box.appendChild(bars((data.runs || {}).by_status, function (k) {
      if (k === "completed") return "ok";
      if (k === "failed") return "bad";
      if (k === "needs_approval") return "warn";
      return "";
    }));

    // Spend (from run_routing)
    var rt = data.routing || {};
    box.appendChild(el("div", "mx-section", "Model spend"));
    box.appendChild(el("div", "mx-spend-sum",
      '<span>' + fmtUsd(rt.total_cost_usd) + ' total</span>' +
      '<span>' + fmtTok(rt.total_tokens) + ' tokens</span>' +
      '<span>' + (rt.total_runs || 0) + ' routed runs</span>' +
      '<span>' + (rt.local_runs || 0) + ' local</span>'));
    box.appendChild(costBars(rt.by_model));

    box.appendChild(el("div", "mx-section", "Spend trend (recent runs)"));
    box.appendChild(spendTrend(rt.trend));
  }

  function connect() {
    try {
      window.PraxisBus.on("run", function () { load(); });
    } catch (_) {}
  }

  function boot() {
    mount = document.getElementById("metrics-mount");
    if (!mount) return;
    ensureOverlay();
    load();
    connect();
    setInterval(load, 8000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
