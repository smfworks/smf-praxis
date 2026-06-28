/* Praxis Work Board — a governed kanban whose lanes are the loop states and
 * whose "Run" executes the card's goal under the broker, reflecting the verdict
 * (done / held / failed) back onto the card. Served from /web/board.js.
 */
(function () {
  "use strict";

  var LANES = ["backlog", "planned", "running", "held", "done", "failed"];
  var LANE_LABEL = {
    backlog: "Backlog", planned: "Planned", running: "Running",
    held: "Held", done: "Done", failed: "Failed"
  };

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
  function statusClass(s) {
    if (s === "completed" || s === "done") return "ok";
    if (s === "failed" || s === "denied") return "bad";
    if (s === "needs_approval" || s === "held") return "warn";
    if (s === "partial" || s === "running") return "info";
    return "muted";
  }

  var mount = null, overlay = null, cards = [];

  function laneCounts() {
    var c = {};
    LANES.forEach(function (l) { c[l] = 0; });
    cards.forEach(function (card) { if (c[card.lane] != null) c[card.lane]++; });
    return c;
  }

  async function load() {
    try {
      var d = await api("/api/board");
      cards = d.cards || [];
      renderPanel();
      if (overlay && overlay.classList.contains("show")) renderBoard();
    } catch (_) { /* keep prior view */ }
  }

  function renderPanel() {
    if (!mount) return;
    mount.innerHTML = "";
    var add = el("div", "wb-add");
    add.innerHTML = '<input class="wb-input" placeholder="New goal\u2026" />' +
      '<button class="wb-addbtn" type="button">Add</button>';
    var input = add.querySelector(".wb-input");
    function doAdd() {
      var v = input.value.trim();
      if (!v) return;
      api("/api/board/create", { title: v, goal: v }).then(function () {
        input.value = "";
        load();
      });
    }
    add.querySelector(".wb-addbtn").onclick = doAdd;
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") doAdd(); });
    mount.appendChild(add);

    var c = laneCounts();
    var summary = el("div", "wb-summary");
    LANES.forEach(function (l) {
      summary.appendChild(el("span", "wb-chip wb-" + l, LANE_LABEL[l] + " " + c[l]));
    });
    mount.appendChild(summary);

    var open = el("button", "wb-open", "Open board \u2922");
    open.type = "button";
    open.onclick = openBoard;
    mount.appendChild(open);
  }

  function ensureOverlay() {
    if (overlay) return;
    overlay = el("div", "wb-overlay");
    overlay.appendChild(el("div", "wb-box"));
    overlay.addEventListener("click", function (e) { if (e.target === overlay) closeBoard(); });
    document.body.appendChild(overlay);
  }
  function openBoard() { ensureOverlay(); overlay.classList.add("show"); renderBoard(); }
  function closeBoard() { if (overlay) overlay.classList.remove("show"); }

  function cardEl(card) {
    var c = el("div", "wb-card");
    c.draggable = true;
    c.innerHTML =
      '<div class="wb-card-title">' + esc(card.title || card.goal) + '</div>' +
      '<div class="wb-card-meta"><span class="wb-dot ' +
      statusClass(card.status || card.lane) + '"></span>' +
      esc(card.status || card.lane) + (card.run_id ? ' \u00b7 trace' : '') + '</div>' +
      '<div class="wb-card-actions">' +
      '<button class="wb-run" type="button" title="Run under governance">\u25b6 Run</button>' +
      (card.run_id ? '<button class="wb-graph" type="button" title="Open Run Graph">Graph</button>' : '') +
      '<button class="wb-del" type="button" title="Delete">\u2715</button></div>';
    c.addEventListener("dragstart", function (e) {
      e.dataTransfer.setData("text/plain", card.card_id);
    });
    c.querySelector(".wb-run").onclick = function (e) {
      e.stopPropagation();
      this.textContent = "\u2026";
      api("/api/board/run", { card_id: card.card_id }).then(load).catch(load);
    };
    c.querySelector(".wb-del").onclick = function (e) {
      e.stopPropagation();
      api("/api/board/delete", { card_id: card.card_id }).then(load);
    };
    var g = c.querySelector(".wb-graph");
    if (g) g.onclick = function (e) {
      e.stopPropagation();
      if (window.PraxisRunGraph) window.PraxisRunGraph.openRun(card.run_id);
    };
    return c;
  }

  function renderBoard() {
    var box = overlay.querySelector(".wb-box");
    box.innerHTML = "";
    var head = el("div", "wb-head");
    head.innerHTML = '<div><b>Work Board</b>' +
      '<span class="wb-sub">drag between lanes \u00b7 Run executes under governance</span></div>';
    var close = el("button", "wb-close", "\u2715");
    close.type = "button";
    close.onclick = closeBoard;
    head.appendChild(close);
    box.appendChild(head);

    var lanesWrap = el("div", "wb-lanes");
    LANES.forEach(function (lane) {
      var col = el("div", "wb-lane");
      col.innerHTML = '<div class="wb-lane-head">' + LANE_LABEL[lane] + '</div>';
      var body = el("div", "wb-lane-body");
      cards.filter(function (c) { return c.lane === lane; })
        .forEach(function (c) { body.appendChild(cardEl(c)); });
      col.appendChild(body);
      col.addEventListener("dragover", function (e) { e.preventDefault(); col.classList.add("wb-over"); });
      col.addEventListener("dragleave", function () { col.classList.remove("wb-over"); });
      col.addEventListener("drop", function (e) {
        e.preventDefault();
        col.classList.remove("wb-over");
        var id = e.dataTransfer.getData("text/plain");
        if (id) api("/api/board/move", { card_id: id, lane: lane }).then(load);
      });
      lanesWrap.appendChild(col);
    });
    box.appendChild(lanesWrap);
  }

  function connect() {
    try {
      window.PraxisBus.on("run", function () { load(); });
    } catch (_) { /* poll keeps it fresh */ }
  }

  function boot() {
    mount = document.getElementById("board-mount");
    if (!mount) return;
    ensureOverlay();
    load();
    connect();
    setInterval(load, 8000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
