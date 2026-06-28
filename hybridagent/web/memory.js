/* Praxis Memory & Knowledge Studio — browse the tiered memory (working /
 * episodic / durable) with provenance + salience, and add or delete entries.
 * Served from /web/memory.js.
 */
(function () {
  "use strict";

  var TIERS = ["working", "episodic", "durable"];

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
  function sal(v) { return typeof v === "number" ? v.toFixed(2) : esc(v); }

  var mount = null, overlay = null, data = null;

  async function load() {
    try {
      data = await api("/api/memory");
      renderPanel();
      if (overlay && overlay.classList.contains("show")) renderStudio();
    } catch (_) { /* keep prior */ }
  }

  function renderPanel() {
    if (!mount || !data) return;
    mount.innerHTML = "";
    var bt = data.by_tier || {};
    var sum = el("div", "mem-summary");
    TIERS.forEach(function (t) {
      sum.appendChild(el("span", "mem-chip mem-" + t, t + " " + (bt[t] || 0)));
    });
    mount.appendChild(sum);
    var open = el("button", "mem-open", "Open studio \u2922");
    open.type = "button";
    open.onclick = openStudio;
    mount.appendChild(open);
  }

  function ensureOverlay() {
    if (overlay) return;
    overlay = el("div", "mem-overlay");
    overlay.appendChild(el("div", "mem-box"));
    overlay.addEventListener("click", function (e) { if (e.target === overlay) closeStudio(); });
    document.body.appendChild(overlay);
  }
  function openStudio() { ensureOverlay(); overlay.classList.add("show"); renderStudio(); }
  function closeStudio() { if (overlay) overlay.classList.remove("show"); }

  function renderStudio() {
    var box = overlay.querySelector(".mem-box");
    box.innerHTML = "";
    var head = el("div", "mem-head");
    head.innerHTML = '<div><b>Memory &amp; Knowledge Studio</b></div>';
    var close = el("button", "mem-close", "\u2715");
    close.type = "button";
    close.onclick = closeStudio;
    head.appendChild(close);
    box.appendChild(head);

    // Add form
    var add = el("div", "mem-add");
    add.innerHTML =
      '<select>' + TIERS.map(function (t) {
        return '<option value="' + t + '"' + (t === "durable" ? " selected" : "") + '>' + t + '</option>';
      }).join("") + '</select>' +
      '<input placeholder="New memory\u2026" />' +
      '<button type="button">Add</button>';
    var sel = add.querySelector("select");
    var input = add.querySelector("input");
    function doAdd() {
      var v = input.value.trim();
      if (!v) return;
      api("/api/memory", { tier: sel.value, text: v, provenance: "dashboard" }).then(function () {
        input.value = "";
        load();
      });
    }
    add.querySelector("button").onclick = doAdd;
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") doAdd(); });
    box.appendChild(add);

    // Tiers
    var items = data.items || [];
    TIERS.forEach(function (t) {
      var inT = items.filter(function (m) { return m.tier === t; });
      box.appendChild(el("div", "mem-section", t + " \u00b7 " + inT.length));
      var list = el("div", "mem-list");
      if (!inT.length) {
        list.appendChild(el("div", "empty", "\u2014"));
      } else {
        inT.forEach(function (m) {
          var row = el("div", "mem-item");
          row.innerHTML =
            '<div class="mem-text">' + esc(m.text) + '</div>' +
            '<div class="mem-meta">' +
            '<span class="mem-prov">' + esc(m.provenance) + '</span>' +
            '<span>sal ' + sal(m.salience) + '</span>' +
            '<span>' + (m.access_count || 0) + ' uses</span>' +
            '<button class="mem-del" type="button" title="Delete">\u2715</button></div>';
          row.querySelector(".mem-del").onclick = function () {
            api("/api/memory/delete", { id: m.id }).then(load);
          };
          list.appendChild(row);
        });
      }
      box.appendChild(list);
    });
  }

  function boot() {
    mount = document.getElementById("memory-mount");
    if (!mount) return;
    ensureOverlay();
    load();
    setInterval(load, 10000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
