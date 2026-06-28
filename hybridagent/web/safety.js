/* Praxis Safety Center — the human-in-the-loop control plane: a live approval
 * queue (approve/deny), the broker kill-switch, and a redacted audit-trail
 * viewer that surfaces egress/taint and policy flags. Served from /web/safety.js.
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
  function verdictClass(v) {
    v = (v || "").toLowerCase();
    if (v.indexOf("allow") >= 0) return "ok";
    if (v.indexOf("deny") >= 0) return "bad";
    if (v.indexOf("approval") >= 0 || v.indexOf("need") >= 0) return "warn";
    return "muted";
  }
  var RULE_LABEL = {
    egress_blocked: "egress", kill_switch_denied: "kill-switch",
    allowlist_denied: "blocked", autonomous_allow: "auto",
    approval_required: "approval", dual_approval: "dual"
  };

  var mount = null, overlay = null, killEngaged = false, pending = [], audit = [];

  async function load() {
    try { var ks = await api("/api/killswitch"); killEngaged = !!ks.engaged; } catch (_) {}
    try { pending = await api("/api/approvals"); } catch (_) { pending = []; }
    renderPanel();
    if (overlay && overlay.classList.contains("show")) renderCenter();
  }
  async function loadAudit() {
    try { var d = await api("/api/audit"); audit = d.entries || []; } catch (_) { audit = []; }
  }

  function renderPanel() {
    if (!mount) return;
    mount.innerHTML = "";
    var ks = el("div", "sf-ks" + (killEngaged ? " on" : ""));
    ks.innerHTML = '<span class="sf-ks-label">Kill-switch</span>' +
      '<button class="sf-ks-btn" type="button">' +
      (killEngaged ? "ENGAGED \u2014 release" : "Engage") + '</button>';
    ks.querySelector(".sf-ks-btn").onclick = function () {
      api("/api/killswitch", { engaged: !killEngaged }).then(load);
    };
    mount.appendChild(ks);

    mount.appendChild(el("div", "sf-pending",
      (pending.length || 0) + " awaiting approval"));

    var open = el("button", "sf-open", "Open Safety Center \u2922");
    open.type = "button";
    open.onclick = openCenter;
    mount.appendChild(open);
  }

  function ensureOverlay() {
    if (overlay) return;
    overlay = el("div", "sf-overlay");
    overlay.appendChild(el("div", "sf-box"));
    overlay.addEventListener("click", function (e) { if (e.target === overlay) closeCenter(); });
    document.body.appendChild(overlay);
  }
  function openCenter() { ensureOverlay(); overlay.classList.add("show"); loadAudit().then(renderCenter); }
  function closeCenter() { if (overlay) overlay.classList.remove("show"); }

  function renderCenter() {
    var box = overlay.querySelector(".sf-box");
    box.innerHTML = "";
    var head = el("div", "sf-head");
    head.innerHTML = '<div><b>Safety Center</b></div>';
    var close = el("button", "sf-close", "\u2715");
    close.type = "button";
    close.onclick = closeCenter;
    head.appendChild(close);
    box.appendChild(head);

    // Kill-switch
    var ksbar = el("div", "sf-ksbar" + (killEngaged ? " on" : ""));
    ksbar.innerHTML = '<span class="desc">' +
      (killEngaged
        ? "Kill-switch ENGAGED \u2014 all send/destructive actions are denied."
        : "Kill-switch released \u2014 consequential actions follow normal approval.") +
      '</span>';
    var ksbtn = el("button", "sf-ks-btn", killEngaged ? "Release" : "Engage");
    ksbtn.type = "button";
    ksbtn.onclick = function () { api("/api/killswitch", { engaged: !killEngaged }).then(load); };
    ksbar.appendChild(ksbtn);
    box.appendChild(el("div", "sf-section", "Kill-switch"));
    box.appendChild(ksbar);

    // Approval queue
    box.appendChild(el("div", "sf-section", "Approval queue (" + pending.length + ")"));
    var q = el("div", "sf-appr");
    if (!pending.length) {
      q.appendChild(el("div", "empty", "Nothing awaiting approval."));
    } else {
      pending.forEach(function (a) {
        var row = el("div", "sf-arow");
        row.innerHTML =
          '<div class="tool">' + esc(a.tool) + '</div>' +
          '<div class="prev">' + esc(a.preview || a.rationale || "") + '</div>' +
          '<div class="acts"><button class="ok" type="button">Approve</button>' +
          '<button class="no" type="button">Deny</button></div>';
        row.querySelector(".ok").onclick = function () {
          api("/api/approve", { approval_id: a.approval_id }).then(load);
        };
        row.querySelector(".no").onclick = function () {
          api("/api/deny", { approval_id: a.approval_id }).then(load);
        };
        q.appendChild(row);
      });
    }
    box.appendChild(q);

    // Audit trail
    box.appendChild(el("div", "sf-section", "Audit trail"));
    var at = el("div", "sf-audit");
    if (!audit.length) {
      at.appendChild(el("div", "empty", "No decisions recorded yet."));
    } else {
      audit.forEach(function (e) {
        var row = el("div", "sf-erow");
        var rule = e.policy_rule || "";
        var badge = RULE_LABEL[rule];
        var flag = badge
          ? '<span class="flag' + (rule === "autonomous_allow" ? " auto" : "") + '">' +
            esc(badge) + '</span>'
          : '';
        var when = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : '';
        row.innerHTML =
          '<span class="v ' + verdictClass(e.verdict) + '">' + esc(e.verdict) + '</span>' +
          '<span class="tool">' + esc(e.tool) + '</span>' +
          flag +
          '<span class="ts">' + esc(when) + '</span>';
        at.appendChild(row);
      });
    }
    box.appendChild(at);
  }

  function connect() {
    try {
      window.PraxisBus.on("run", function () { load(); });
    } catch (_) { /* poll keeps it fresh */ }
  }

  function boot() {
    mount = document.getElementById("safety-mount");
    if (!mount) return;
    ensureOverlay();
    load();
    connect();
    setInterval(load, 8000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
