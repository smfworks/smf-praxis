/* Praxis Run Graph — durable, replayable governed-loop DAG.
 *
 * First module of the modular dashboard shell: served from /web/run-graph.js
 * (not inlined). Lists recent run traces from /api/traces, and on click renders
 * the plan as a dependency DAG plus a replayable event timeline from
 * /api/traces/{run_id}. Subscribes to the SSE bus for live "run" events so the
 * graph updates as the governed loop executes.
 */
(function () {
  "use strict";

  var STATUS = {
    step_done: "done", step_held: "held", step_denied: "denied",
    step_failed: "failed", step_skipped: "skipped"
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
  function fmtAgo(ts) {
    if (!ts) return "";
    var s = Math.max(0, (Date.now() / 1000) - ts);
    if (s < 60) return Math.floor(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function statusClass(s) {
    if (s === "completed" || s === "done") return "ok";
    if (s === "failed" || s === "denied") return "bad";
    if (s === "needs_approval" || s === "held") return "warn";
    if (s === "partial" || s === "running") return "info";
    return "muted";
  }
  async function getJSON(url) {
    var r = await fetch(url);
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }

  var list = null, overlay = null, openRunId = null;

  async function loadRuns() {
    if (!list) return;
    try {
      var data = await getJSON("/api/traces");
      var runs = (data && data.runs) || [];
      if (!runs.length) {
        list.innerHTML = '<div class="empty">No runs yet.</div>';
        return;
      }
      list.innerHTML = "";
      runs.forEach(function (r) {
        var row = el("button", "rg-run");
        row.type = "button";
        row.innerHTML =
          '<span class="rg-dot ' + statusClass(r.status) + '"></span>' +
          '<span class="rg-goal">' + esc(r.goal || r.run_id) + '</span>' +
          '<span class="rg-meta">' + esc(r.status || "") + ' \u00b7 ' +
          (r.event_count || 0) + ' ev \u00b7 ' + fmtAgo(r.started_ts) + '</span>';
        row.onclick = function () { openRun(r.run_id); };
        list.appendChild(row);
      });
    } catch (_) { /* daemon may be starting; keep prior view */ }
  }

  function computeModel(events) {
    var nodes = {}, order = [], overall = "";
    events.forEach(function (ev) {
      var d = ev.data || {};
      if (ev.kind === "plan") {
        (d.nodes || []).forEach(function (n) {
          if (!nodes[n.id]) {
            nodes[n.id] = {
              id: n.id, tool: n.tool, intent: n.intent,
              depends_on: n.depends_on || [], status: "pending"
            };
            order.push(n.id);
          }
        });
      } else if (ev.kind === "final") {
        overall = d.status || "";
      } else if (STATUS[ev.kind] && d.id) {
        if (!nodes[d.id]) {
          nodes[d.id] = {
            id: d.id, tool: d.tool || "", intent: d.intent || "",
            depends_on: [], status: "pending"
          };
          order.push(d.id);
        }
        nodes[d.id].status = STATUS[ev.kind];
      }
    });
    return { nodes: nodes, order: order, overall: overall };
  }

  function layout(nodes, order) {
    var depth = {};
    function dof(id, seen) {
      if (depth[id] != null) return depth[id];
      seen = seen || {};
      if (seen[id]) return 0;
      seen[id] = true;
      var n = nodes[id], m = 0;
      ((n && n.depends_on) || []).forEach(function (p) {
        if (nodes[p]) m = Math.max(m, dof(p, seen) + 1);
      });
      depth[id] = m;
      return m;
    }
    order.forEach(function (id) { dof(id); });
    var cols = {};
    order.forEach(function (id) {
      var k = depth[id] || 0;
      (cols[k] = cols[k] || []).push(id);
    });
    var pos = {}, colW = 184, rowH = 64, padX = 22, padY = 22, maxRow = 0, maxCol = 0;
    Object.keys(cols).forEach(function (k) {
      var col = +k;
      maxCol = Math.max(maxCol, col);
      cols[k].forEach(function (id, i) {
        pos[id] = { x: padX + col * colW, y: padY + i * rowH };
        maxRow = Math.max(maxRow, i);
      });
    });
    return { pos: pos, w: padX * 2 + (maxCol + 1) * colW, h: padY * 2 + (maxRow + 1) * rowH };
  }

  function svgGraph(model) {
    var lay = layout(model.nodes, model.order);
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "rg-svg");
    svg.setAttribute("viewBox", "0 0 " + Math.max(lay.w, 200) + " " + Math.max(lay.h, 120));
    model.order.forEach(function (id) {
      var n = model.nodes[id], p = lay.pos[id];
      if (!p) return;
      (n.depends_on || []).forEach(function (dep) {
        var q = lay.pos[dep];
        if (!q) return;
        var path = document.createElementNS(NS, "path");
        var x1 = q.x + 156, y1 = q.y + 18, x2 = p.x, y2 = p.y + 18, mx = (x1 + x2) / 2;
        path.setAttribute("d", "M" + x1 + " " + y1 + " C " + mx + " " + y1 +
          " " + mx + " " + y2 + " " + x2 + " " + y2);
        path.setAttribute("class", "rg-edge");
        svg.appendChild(path);
      });
    });
    model.order.forEach(function (id) {
      var n = model.nodes[id], p = lay.pos[id];
      if (!p) return;
      var g = document.createElementNS(NS, "g");
      g.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
      g.setAttribute("class", "rg-node rg-" + n.status);
      var rect = document.createElementNS(NS, "rect");
      rect.setAttribute("width", "156");
      rect.setAttribute("height", "36");
      rect.setAttribute("rx", "8");
      g.appendChild(rect);
      var t1 = document.createElementNS(NS, "text");
      t1.setAttribute("x", "10"); t1.setAttribute("y", "15"); t1.setAttribute("class", "rg-t1");
      t1.textContent = n.tool || n.id;
      g.appendChild(t1);
      var t2 = document.createElementNS(NS, "text");
      t2.setAttribute("x", "10"); t2.setAttribute("y", "28"); t2.setAttribute("class", "rg-t2");
      t2.textContent = n.id + " \u00b7 " + n.status;
      g.appendChild(t2);
      var title = document.createElementNS(NS, "title");
      title.textContent = (n.intent || n.id) + " \u2014 " + n.status;
      g.appendChild(title);
      svg.appendChild(g);
    });
    return svg;
  }

  function renderOverlay(run, events) {
    var model = computeModel(events);
    var box = overlay.querySelector(".rg-box");
    box.innerHTML = "";
    var head = el("div", "rg-head");
    head.innerHTML = '<div><b>' + esc((run && run.goal) || openRunId) + '</b>' +
      '<span class="rg-sub">' + esc((run && run.status) || model.overall || "") + '</span></div>';
    var close = el("button", "rg-close", "\u2715");
    close.type = "button";
    close.onclick = closeRun;
    head.appendChild(close);
    box.appendChild(head);

    var graphWrap = el("div", "rg-graphwrap");
    graphWrap.appendChild(svgGraph(model));
    box.appendChild(graphWrap);

    var tl = el("div", "rg-timeline");
    events.forEach(function (ev) {
      var d = ev.data || {};
      var r = el("div", "rg-evt");
      r.innerHTML =
        '<span class="rg-seq">' + esc(ev.seq) + '</span>' +
        '<span class="rg-kind">' + esc(ev.kind) + '</span>' +
        '<span class="rg-evnode">' + esc(d.id || "") + '</span>' +
        '<span class="rg-evdet">' +
        esc(d.reason || d.intent || d.preview || d.status || "") + '</span>';
      tl.appendChild(r);
    });
    box.appendChild(tl);
  }

  function ensureOverlay() {
    if (overlay) return;
    overlay = el("div", "rg-overlay");
    overlay.appendChild(el("div", "rg-box"));
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) closeRun();
    });
    document.body.appendChild(overlay);
  }

  async function openRun(id) {
    openRunId = id;
    ensureOverlay();
    overlay.classList.add("show");
    try {
      var data = await getJSON("/api/traces/" + encodeURIComponent(id));
      renderOverlay(data.run, data.events || []);
    } catch (_) {
      overlay.querySelector(".rg-box").innerHTML =
        '<div class="rg-head"><b>Run unavailable</b>' +
        '<button class="rg-close" type="button">\u2715</button></div>';
      overlay.querySelector(".rg-close").onclick = closeRun;
    }
  }
  function closeRun() {
    openRunId = null;
    if (overlay) overlay.classList.remove("show");
  }

  function connect() {
    try {
      window.PraxisBus.on("run", function (e) {
        var ev;
        try { ev = JSON.parse(e.data); } catch (_) { return; }
        var p = ev.payload || {};
        loadRuns();
        if (openRunId && p.run_id === openRunId) openRun(openRunId);
      });
    } catch (_) { /* SSE unsupported — interval poll keeps the list fresh */ }
  }

  function boot() {
    list = document.getElementById("runlist");
    ensureOverlay();
    loadRuns();
    connect();
    setInterval(loadRuns, 6000);
    // Let other modules (e.g. the Work Board) open a run's DAG.
    window.PraxisRunGraph = { openRun: openRun, refresh: loadRuns };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
