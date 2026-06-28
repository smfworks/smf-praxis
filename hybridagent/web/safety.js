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
  function complianceClass() {
    var m = compliance.mode || "enforced";
    if (m === "permissive") return " bad";
    if (m === "autonomous") return " warn";
    return "";
  }
  function setCompliance(mode, ttlSeconds) {
    var body = { mode: mode };
    if (ttlSeconds != null) body.ttl_seconds = ttlSeconds;
    api("/api/compliance", body).then(function (res) {
      if (!(res && res.error)) load();
    }).catch(function () {});
  }
  function fmtSecs(s) {
    s = Math.max(0, Math.floor(s));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return h + "h" + (m ? " " + m + "m" : "");
    if (m) return m + "m";
    return sec + "s";
  }
  var RULE_LABEL = {
    egress_blocked: "egress", kill_switch_denied: "kill-switch",
    allowlist_denied: "blocked", autonomous_allow: "auto",
    approval_required: "approval", dual_approval: "dual"
  };

  var mount = null, overlay = null, killEngaged = false, pending = [], audit = [];
  var compliance = { mode: "enforced", modes: [] };

  async function load() {
    try { var ks = await api("/api/killswitch"); killEngaged = !!ks.engaged; } catch (_) {}
    try { compliance = await api("/api/compliance"); } catch (_) {}
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

    var cmp = el("div", "sf-cmp" + complianceClass());
    var cmpVal = esc(compliance.mode || "enforced");
    if (compliance.expires_in_seconds != null) cmpVal += " (" + fmtSecs(compliance.expires_in_seconds) + ")";
    cmp.innerHTML = '<span class="sf-cmp-label">Compliance</span>' +
      '<span class="sf-cmp-val">' + cmpVal + '</span>';
    mount.appendChild(cmp);

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

    // Compliance mode selector
    box.appendChild(el("div", "sf-section", "Compliance mode"));
    var modes = el("div", "sf-cmp-modes");
    (compliance.modes || []).forEach(function (m) {
      var opt = el("button", "sf-cmp-opt" + (m.active ? " active" : ""));
      opt.type = "button";
      opt.setAttribute("data-mode", m.id);
      opt.innerHTML = '<span class="nm"><span class="dot"></span>' + esc(m.label) +
        (m.active ? " \u2713" : "") + '</span><span class="ds">' + esc(m.description) + "</span>";
      opt.onclick = function () { setCompliance(m.id); };
      modes.appendChild(opt);
    });
    box.appendChild(modes);
    var cmode = compliance.mode || "enforced";
    if (cmode !== "enforced") {
      var warn = el("div", "sf-cmp-warn" + (cmode === "permissive" ? " bad" : ""));
      warn.textContent = cmode === "permissive"
        ? "Permissive: consequential actions run unsupervised and the egress/injection guards are OFF \u2014 only the kill-switch remains. Use in trusted or sandboxed environments only."
        : "Autonomous: consequential actions run without approval. The egress firewall, injection detection, and kill-switch all remain active.";
      box.appendChild(warn);

      var ttlRow = el("div", "sf-cmp-ttl");
      ttlRow.appendChild(el("span", "sf-cmp-ttl-label", "Auto-revert:"));
      [["Off", 0], ["15 min", 900], ["1 hour", 3600], ["4 hours", 14400]].forEach(function (o) {
        var chip = el("button", "sf-cmp-chip");
        chip.type = "button";
        chip.textContent = o[0];
        chip.onclick = function () { setCompliance(cmode, o[1]); };
        ttlRow.appendChild(chip);
      });
      box.appendChild(ttlRow);
      if (compliance.expires_in_seconds != null) {
        box.appendChild(el("div", "sf-cmp-count",
          "Reverts to enforced in " + fmtSecs(compliance.expires_in_seconds) + "."));
      }
    }

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
