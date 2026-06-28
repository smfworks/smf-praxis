/* Praxis Inference Control — current model/provider, the role-routing vocabulary
 * and learned-router state, and an *enforceable* spend budget (a cap that halts
 * runs, not just a number you watch). Served from /web/inference.js.
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
  async function api(url, body) {
    var opt = body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : undefined;
    var r = await fetch(url, opt);
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }
  function money(n) { return "$" + (Number(n) || 0).toFixed(2); }
  function budgetClass(b) {
    if (!b || !b.limit_usd) return "";
    var pct = b.spent_usd / b.limit_usd;
    if (pct >= 1) return "bad";
    if (pct >= 0.8) return "warn";
    return "";
  }
  function budgetPct(b) {
    if (!b || !b.limit_usd) return 0;
    return Math.min(100, Math.round((b.spent_usd / b.limit_usd) * 100));
  }

  var mount = null, overlay = null, info = null;

  async function load() {
    try {
      info = await api("/api/inference");
      renderPanel();
      if (overlay && overlay.classList.contains("show")) renderOverlay();
    } catch (_) { window.PraxisPanelError(mount, "Inference", load); }
  }

  function renderPanel() {
    if (!mount || !info) return;
    mount.innerHTML = "";
    var m = info.model || {};
    var b = info.budget || {};
    var model = el("div", "if-model");
    model.innerHTML = esc(m.model || "\u2014") +
      '<span class="tag' + (m.configured ? " live" : "") + '">' +
      (m.configured ? "live" : "mock") + '</span>';
    mount.appendChild(model);

    var hud = el("div", "if-hud");
    var capLabel = b.limit_usd ? money(b.spent_usd) + " / " + money(b.limit_usd) : "no cap set";
    hud.innerHTML =
      '<div class="row"><span>Spend</span><span>' + esc(capLabel) + '</span></div>' +
      '<div class="if-track"><span class="if-fill ' + budgetClass(b) +
      '" style="width:' + budgetPct(b) + '%"></span></div>';
    mount.appendChild(hud);

    var open = el("button", "if-open", "Open inference control \u2922");
    open.type = "button";
    open.onclick = openOverlay;
    mount.appendChild(open);
  }

  function ensureOverlay() {
    if (overlay) return;
    overlay = el("div", "if-overlay");
    overlay.appendChild(el("div", "if-box"));
    overlay.addEventListener("click", function (e) { if (e.target === overlay) closeOverlay(); });
    document.body.appendChild(overlay);
  }
  function openOverlay() { ensureOverlay(); overlay.classList.add("show"); renderOverlay(); }
  function closeOverlay() { if (overlay) overlay.classList.remove("show"); }

  var ROLE_TOOLS = {
    researcher: "events, mail, files, notes",
    drafter: "mail, draft, send",
    compliance: "events, mail, files, notes",
    predictor: "events, mail, notes"
  };

  function renderOverlay() {
    var box = overlay.querySelector(".if-box");
    box.innerHTML = "";
    var head = el("div", "if-head");
    head.innerHTML = '<div><b>Inference Control</b></div>';
    var close = el("button", "if-close", "\u2715");
    close.type = "button";
    close.onclick = closeOverlay;
    head.appendChild(close);
    box.appendChild(head);

    var m = info.model || {};
    box.appendChild(el("div", "if-section", "Model"));
    box.appendChild(el("div", "if-kv", '<span class="k">Default model</span><span class="v">' + esc(m.model || "\u2014") + '</span>'));
    box.appendChild(el("div", "if-kv", '<span class="k">Provider</span><span class="v">' + (m.configured ? "configured (live)" : "mock (offline)") + '</span>'));
    box.appendChild(el("div", "if-kv", '<span class="k">Embedding model</span><span class="v">' + esc(m.embed_model || "\u2014") + '</span>'));

    var r = info.router || {};
    box.appendChild(el("div", "if-section",
      "Role routing \u00b7 " + (r.trained ? "learned (" + r.n_samples + " samples)" : "heuristic")));
    var roles = el("div", "if-roles");
    (r.roles || []).forEach(function (name) {
      var card = el("div", "if-role");
      card.innerHTML = '<div class="name">' + esc(name) + '</div>' +
        '<div class="tools">' + esc(ROLE_TOOLS[name] || "") + '</div>';
      roles.appendChild(card);
    });
    box.appendChild(roles);

    var b = info.budget || {};
    box.appendChild(el("div", "if-section", "Spend budget (enforced)"));
    var bud = el("div", "if-budget" + (b.over ? " over" : ""));
    var cap = b.limit_usd
      ? money(b.spent_usd) + " spent of " + money(b.limit_usd) + " cap \u00b7 " + (b.runs || 0) + " runs"
      : "No cap \u2014 runs are not budget-limited (" + (b.runs || 0) + " runs, " + money(b.spent_usd) + " est.)";
    bud.appendChild(el("div", "big", esc(cap)));
    if (b.over) bud.appendChild(el("div", "warnmsg", "\u26d4 Budget reached \u2014 new runs are blocked until you raise or reset the cap."));
    var ctrls = el("div", "ctrls");
    ctrls.innerHTML = '<input type="number" min="0" step="0.5" placeholder="cap $" />' +
      '<button class="primary" type="button">Set cap</button>' +
      '<button type="button">Reset spend</button>';
    var input = ctrls.querySelector("input");
    if (b.limit_usd) input.value = b.limit_usd;
    ctrls.querySelector(".primary").onclick = function () {
      api("/api/budget", { limit_usd: parseFloat(input.value) || 0 }).then(load);
    };
    ctrls.querySelectorAll("button")[1].onclick = function () {
      api("/api/budget", { reset: true }).then(load);
    };
    bud.appendChild(ctrls);
    box.appendChild(bud);

    var routes = info.routes || [];
    if (routes.length) {
      box.appendChild(el("div", "if-section", "Recent routing"));
      routes.forEach(function (rt) {
        var tok = (Number(rt.prompt_tokens) || 0) + (Number(rt.completion_tokens) || 0);
        var where = rt.local ? "local" : "cloud";
        var fb = (Number(rt.fallbacks) || 0) > 0 ? " \u00b7 " + rt.fallbacks + " fallback" : "";
        var reason = String(rt.escalation_reason || "");
        var escd = (Number(rt.escalations) || 0) > 0
          ? " \u00b7 \u26a1 escalated" + (reason && reason !== "escalated" ? " (" + esc(reason) + ")" : "")
          : "";
        var v = esc(rt.model || "mock") + ' <span class="tag">' + where + "</span> \u00b7 " +
          tok + " tok \u00b7 $" + (Number(rt.cost_usd) || 0).toFixed(4) + fb + escd;
        box.appendChild(el("div", "if-kv",
          '<span class="k">' + esc(String(rt.goal || rt.run_id || "").slice(0, 28)) + "</span>" +
          '<span class="v">' + v + "</span>"));
      });
    }
  }

  function connect() {
    try {
      window.PraxisBus.on("run", function () { load(); });
      window.PraxisBus.on("alert", function () { load(); });
    } catch (_) {}
  }

  function boot() {
    mount = document.getElementById("inference-mount");
    if (!mount) return;
    ensureOverlay();
    load();
    connect();
    setInterval(load, 8000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
