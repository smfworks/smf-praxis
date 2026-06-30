/* Praxis Knowledge panel — manage RAG repositories / the LLM wiki.
 *
 * Register knowledge sources (a folder/file path or an http(s) URL), see each
 * source's namespace, index status, and indexed-chunk counts, re-index on
 * demand, and remove a source. Backed by /api/sources (GET/POST),
 * /api/sources/delete, /api/sources/refresh. Served from /web/knowledge.js.
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
  function when(ts) {
    if (!ts) return "never";
    var d = (Date.now() / 1000) - ts;
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + "m ago";
    if (d < 86400) return Math.floor(d / 3600) + "h ago";
    return Math.floor(d / 86400) + "d ago";
  }

  var mount = null, overlay = null, data = null, busy = false;

  async function load() {
    try {
      data = await api("/api/sources");
      renderPanel();
      if (overlay && overlay.classList.contains("show")) renderStudio();
    } catch (_) { window.PraxisPanelError(mount, "Knowledge", load); }
  }

  function renderPanel() {
    if (!mount || !data) return;
    mount.innerHTML = "";
    var stats = data.stats || {};
    var n = (data.sources || []).length;
    var sum = el("div", "kb-summary");
    sum.appendChild(el("span", "kb-chip", n + " source" + (n === 1 ? "" : "s")));
    sum.appendChild(el("span", "kb-chip", (stats.chunks || 0) + " chunks"));
    sum.appendChild(el("span", "kb-chip", (stats.docs || 0) + " docs"));
    mount.appendChild(sum);
    var open = el("button", "kb-open", "Manage sources \u2922");
    open.type = "button";
    open.onclick = openStudio;
    mount.appendChild(open);
  }

  function ensureOverlay() {
    if (overlay) return;
    overlay = el("div", "kb-overlay");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-label", "Knowledge sources");
    overlay.appendChild(el("div", "kb-box"));
    overlay.addEventListener("click", function (e) { if (e.target === overlay) closeStudio(); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && overlay.classList.contains("show")) closeStudio();
    });
    document.body.appendChild(overlay);
  }
  function openStudio() { ensureOverlay(); overlay.classList.add("show"); renderStudio(); }
  function closeStudio() { if (overlay) overlay.classList.remove("show"); }

  function renderStudio() {
    var box = overlay.querySelector(".kb-box");
    box.innerHTML = "";
    var head = el("div", "kb-head");
    head.innerHTML = '<div><b>Knowledge sources</b> <span class="kb-sub">RAG repositories &amp; LLM wiki</span></div>';
    var close = el("button", "kb-close", "\u2715");
    close.type = "button"; close.onclick = closeStudio;
    head.appendChild(close);
    box.appendChild(head);

    // Add form: URI + namespace + optional title + refresh hours.
    var add = el("div", "kb-add");
    add.innerHTML =
      '<input class="kb-uri" placeholder="Folder path, file, or https:// URL\u2026" />' +
      '<input class="kb-ns" placeholder="namespace (e.g. kb)" value="kb" />' +
      '<input class="kb-title" placeholder="title (optional)" />' +
      '<input class="kb-hrs" type="number" min="0" step="1" placeholder="refresh h" />' +
      '<button class="kb-addbtn" type="button">Add &amp; index</button>';
    var uri = add.querySelector(".kb-uri");
    var ns = add.querySelector(".kb-ns");
    var title = add.querySelector(".kb-title");
    var hrs = add.querySelector(".kb-hrs");
    var addbtn = add.querySelector(".kb-addbtn");
    function doAdd() {
      var u = uri.value.trim();
      if (!u || busy) return;
      busy = true; addbtn.disabled = true; addbtn.textContent = "Indexing\u2026";
      var body = { uri: u, ns: ns.value.trim() || "kb", title: title.value.trim() };
      var h = parseFloat(hrs.value);
      if (!isNaN(h) && h > 0) body.refresh_hours = h;
      api("/api/sources", body).then(function (res) {
        busy = false; addbtn.disabled = false; addbtn.textContent = "Add & index";
        if (res.error) { window.PraxisToast && window.PraxisToast("Source error: " + res.error, "error"); return; }
        uri.value = ""; title.value = "";
        window.PraxisToast && window.PraxisToast("Indexed " + (res.source_id || "source"), "ok");
        load();
      }).catch(function (e) {
        busy = false; addbtn.disabled = false; addbtn.textContent = "Add & index";
        window.PraxisToast && window.PraxisToast("Add failed: " + e, "error");
      });
    }
    addbtn.onclick = doAdd;
    uri.addEventListener("keydown", function (e) { if (e.key === "Enter") doAdd(); });
    box.appendChild(add);
    box.appendChild(el("div", "kb-hint",
      "Folders and files index immediately. URLs are fetched safely "
      + "(http/https only; private hosts blocked unless opted in). Set a refresh "
      + "interval to keep a source current automatically."));

    // Source list
    var list = el("div", "kb-list");
    var sources = data.sources || [];
    if (!sources.length) {
      list.appendChild(el("div", "empty", "No knowledge sources yet \u2014 add one above."));
    } else {
      var byNs = data.by_ns || {};
      sources.forEach(function (s) {
        var row = el("div", "kb-item kb-st-" + esc(s.status));
        var nsStat = byNs[s.ns] || {};
        var meta =
          '<span class="kb-ns-tag">' + esc(s.ns) + '</span>' +
          '<span class="kb-type">' + esc(s.source_type) + '</span>' +
          '<span class="kb-status">' + esc(s.status) + '</span>' +
          '<span>' + when(s.last_ingested_ts) + '</span>' +
          (s.refresh_interval_seconds
            ? '<span>auto ' + Math.round(s.refresh_interval_seconds / 3600) + 'h</span>'
            : '') +
          '<span class="kb-ns-size">' + (nsStat.chunks || 0) + ' chunks</span>';
        row.innerHTML =
          '<div class="kb-row-top">' +
          '<div class="kb-name" title="' + esc(s.uri) + '">' + esc(s.title || s.uri) + '</div>' +
          '<div class="kb-actions">' +
          '<button class="kb-reindex" type="button" title="Re-index">\u21bb</button>' +
          '<button class="kb-del" type="button" title="Remove">\u2715</button>' +
          '</div></div>' +
          '<div class="kb-meta">' + meta + '</div>' +
          (s.error ? '<div class="kb-err">' + esc(s.error) + '</div>' : '');
        row.querySelector(".kb-reindex").onclick = function () {
          api("/api/sources/refresh", { source_id: s.source_id }).then(function (res) {
            if (res.error) window.PraxisToast && window.PraxisToast(res.error, "error");
            else window.PraxisToast && window.PraxisToast("Re-indexed", "ok");
            load();
          });
        };
        row.querySelector(".kb-del").onclick = function () {
          api("/api/sources/delete", { source_id: s.source_id }).then(load);
        };
        list.appendChild(row);
      });
    }
    box.appendChild(list);

    var foot = el("div", "kb-foot");
    var refreshAll = el("button", "kb-refresh-all", "Refresh all due");
    refreshAll.type = "button";
    refreshAll.onclick = function () {
      api("/api/sources/refresh", {}).then(function () {
        window.PraxisToast && window.PraxisToast("Refreshed due sources", "ok");
        load();
      });
    };
    foot.appendChild(refreshAll);
    box.appendChild(foot);
  }

  function boot() {
    mount = document.getElementById("knowledge-mount");
    if (!mount) return;
    ensureOverlay();
    load();
    setInterval(load, 12000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
