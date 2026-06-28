/* Praxis Command Palette — Ctrl/Cmd+K opens a single launcher to run any command
 * (jump to any panel/overlay, new chat, engage the kill-switch) or search across
 * memory, run traces, board cards, and the audit trail. Served from /web/palette.js.
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
  function clickSel(sel) {
    var e = document.querySelector(sel);
    if (e) e.click();
    close();
  }
  function openRun(id) {
    close();
    if (window.PraxisRunGraph) window.PraxisRunGraph.openRun(id);
  }

  var COMMANDS = [
    { label: "Open Work Board", hint: "kanban", run: function () { clickSel("#board-mount .wb-open"); } },
    { label: "Open Safety Center", hint: "approvals \u00b7 kill-switch \u00b7 audit", run: function () { clickSel("#safety-mount .sf-open"); } },
    { label: "Open Inference Control", hint: "model \u00b7 router \u00b7 budget", run: function () { clickSel("#inference-mount .if-open"); } },
    { label: "Open Observability", hint: "eval trend \u00b7 metrics", run: function () { clickSel("#metrics-mount .mx-open"); } },
    { label: "Open Memory Studio", hint: "tiers \u00b7 provenance", run: function () { clickSel("#memory-mount .mem-open"); } },
    { label: "Open latest Run Graph", hint: "trace DAG", run: function () { clickSel("#runlist .rg-run"); } },
    { label: "New chat", hint: "start fresh", run: function () { if (window.newChat) window.newChat(); close(); } },
    { label: "Engage / release kill-switch", hint: "halt consequential actions", run: function () { clickSel("#safety-mount .sf-ks-btn"); } },
    { label: "Open Settings", hint: "keys \u00b7 config \u00b7 version", run: function () { if (window.PraxisSettings) window.PraxisSettings.open(); close(); } }
  ];

  var overlay = null, input = null, listEl = null, items = [], active = 0, timer = null;

  function ensure() {
    if (overlay) return;
    overlay = el("div", "pl-overlay");
    var box = el("div", "pl-box");
    box.innerHTML =
      '<input class="pl-input" placeholder="Type a command, or search memory / runs / audit\u2026" />' +
      '<div class="pl-list"></div>';
    overlay.appendChild(box);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
    input = box.querySelector(".pl-input");
    listEl = box.querySelector(".pl-list");
    input.addEventListener("input", onInput);
    input.addEventListener("keydown", onKey);
  }
  function open() {
    ensure();
    overlay.classList.add("show");
    input.value = "";
    render(filterCommands(""));
    setTimeout(function () { input.focus(); }, 20);
  }
  function close() { if (overlay) overlay.classList.remove("show"); }
  function toggle() {
    if (overlay && overlay.classList.contains("show")) close();
    else open();
  }

  function filterCommands(q) {
    q = q.toLowerCase();
    return COMMANDS.filter(function (c) {
      return !q || c.label.toLowerCase().indexOf(q) >= 0 || (c.hint || "").indexOf(q) >= 0;
    }).map(function (c) {
      return { type: "cmd", label: c.label, hint: c.hint, run: c.run };
    });
  }

  function onInput() {
    var q = input.value.trim();
    var cmds = filterCommands(q);
    render(cmds);
    if (timer) clearTimeout(timer);
    if (q.length >= 2) timer = setTimeout(function () { doSearch(q, cmds); }, 180);
  }

  async function doSearch(q, cmds) {
    try {
      var r = await getJSON("/api/search?q=" + encodeURIComponent(q));
      var out = [];
      (r.runs || []).forEach(function (x) {
        out.push({ type: "run", label: x.goal || x.run_id, hint: "run \u00b7 " + (x.status || ""),
          run: function () { openRun(x.run_id); } });
      });
      (r.cards || []).forEach(function (x) {
        out.push({ type: "card", label: x.title || x.goal, hint: "card \u00b7 " + (x.lane || ""),
          run: function () { clickSel("#board-mount .wb-open"); } });
      });
      (r.memory || []).forEach(function (x) {
        out.push({ type: "mem", label: x.text, hint: "memory \u00b7 " + (x.tier || ""),
          run: function () { clickSel("#memory-mount .mem-open"); } });
      });
      (r.audit || []).forEach(function (x) {
        out.push({ type: "audit", label: x.tool + " \u2192 " + x.verdict,
          hint: "audit" + (x.policy_rule ? " \u00b7 " + x.policy_rule : ""),
          run: function () { clickSel("#safety-mount .sf-open"); } });
      });
      // Only repaint if the query is still current.
      if (input.value.trim() === q) render(cmds.concat(out));
    } catch (_) { /* ignore */ }
  }

  function render(arr) {
    items = arr;
    active = 0;
    listEl.innerHTML = "";
    if (!arr.length) {
      listEl.appendChild(el("div", "pl-empty", "No matches."));
      return;
    }
    arr.forEach(function (it, i) {
      var row = el("div", "pl-item" + (i === 0 ? " active" : ""));
      row.innerHTML =
        '<span class="pl-badge pl-' + it.type + '">' + it.type + '</span>' +
        '<span class="pl-label">' + esc(it.label) + '</span>' +
        '<span class="pl-hint">' + esc(it.hint || "") + '</span>';
      row.onclick = function () { it.run(); };
      row.onmouseenter = function () { setActive(i); };
      listEl.appendChild(row);
    });
  }
  function setActive(i) {
    active = i;
    var kids = listEl.children;
    for (var k = 0; k < kids.length; k++) kids[k].classList.toggle("active", k === i);
    var a = kids[i];
    if (a && a.scrollIntoView) a.scrollIntoView({ block: "nearest" });
  }

  function onKey(e) {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(Math.min(active + 1, items.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(Math.max(active - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); if (items[active]) items[active].run(); }
    else if (e.key === "Escape") { close(); }
  }

  function boot() {
    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        toggle();
      }
    });
    var btn = document.getElementById("cmdk");
    if (btn) btn.onclick = open;
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
