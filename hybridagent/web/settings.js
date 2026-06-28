/* Praxis Settings — one overlay for general info and API-key management (OS
 * keychain / gitignored file). Opened from the header gear button. Served from
 * /web/settings.js. */
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
    return r.json();
  }
  function toast(msg) { if (window.showToast) window.showToast(msg); }

  var overlay = null, data = { providers: [], key_providers: [] };

  function ensure() {
    if (overlay) return;
    overlay = el("div", "st-overlay");
    overlay.appendChild(el("div", "st-box"));
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && overlay.classList.contains("show")) close();
    });
    document.body.appendChild(overlay);
  }
  async function open() { ensure(); overlay.classList.add("show"); await load(); }
  function close() { if (overlay) overlay.classList.remove("show"); }

  async function load() {
    try { data = await api("/api/secrets"); }
    catch (_) { data = { providers: [], key_providers: [] }; }
    render();
  }

  function render() {
    var box = overlay.querySelector(".st-box");
    box.innerHTML = "";
    var head = el("div", "st-head", "<div><b>Settings</b></div>");
    var x = el("button", "st-close", "\u2715"); x.type = "button"; x.onclick = close;
    head.appendChild(x); box.appendChild(head);

    box.appendChild(el("div", "st-section", "General"));
    var gen = el("div", "st-grid");
    gen.innerHTML =
      '<div class="st-k">Version</div><div class="st-v">' + esc(data.version || "?") + "</div>" +
      '<div class="st-k">Config</div><div class="st-v mono">' + esc(data.config_path || "") + "</div>" +
      '<div class="st-k">Keychain</div><div class="st-v">' +
      (data.keychain_available ? "available" : "unavailable (using gitignored file)") + "</div>";
    box.appendChild(gen);

    box.appendChild(el("div", "st-section", "API keys"));
    if (data.keychain_available) {
      var mig = el("button", "st-btn", "Migrate plaintext keys \u2192 keychain");
      mig.type = "button";
      mig.onclick = function () {
        api("/api/secrets", { action: "migrate" }).then(function (r) {
          if (r.error) { toast(r.error); return; }
          data = r; render(); toast("Migrated " + (r.migrated || 0) + " key(s).");
        });
      };
      box.appendChild(mig);
    }
    var keys = el("div", "st-keys");
    if (!(data.providers || []).length) {
      keys.appendChild(el("div", "empty", "No providers configured yet."));
    } else {
      data.providers.forEach(function (p) {
        var row = el("div", "st-krow");
        row.innerHTML = '<span class="st-prov">' + esc(p.label || p.id) +
          '</span><span class="st-loc">' + esc(p.location) + "</span>";
        var rm = el("button", "st-rm", "Remove"); rm.type = "button";
        rm.onclick = function () {
          api("/api/secrets", { action: "delete", provider: p.id }).then(function (r) {
            if (r.error) { toast(r.error); return; }
            data = r; render(); toast("Removed key for " + p.id);
          });
        };
        row.appendChild(rm);
        keys.appendChild(row);
      });
    }
    box.appendChild(keys);

    box.appendChild(el("div", "st-section", "Add or update a key"));
    var form = el("div", "st-form");
    var sel = el("select", "st-sel");
    (data.key_providers || []).forEach(function (kp) {
      var o = el("option"); o.value = kp.id; o.textContent = kp.label || kp.id; sel.appendChild(o);
    });
    var inp = el("input", "st-input"); inp.type = "password"; inp.placeholder = "Paste API key";
    var save = el("button", "st-btn primary", "Save"); save.type = "button";
    save.onclick = function () {
      var key = inp.value.trim();
      if (!key) { toast("Enter a key."); return; }
      api("/api/secrets", { action: "set", provider: sel.value, key: key }).then(function (r) {
        if (r.error) { toast(r.error); return; }
        inp.value = ""; data = r; render(); toast("Stored key in " + (r.backend || "store"));
      });
    };
    form.appendChild(sel); form.appendChild(inp); form.appendChild(save);
    box.appendChild(form);
    box.appendChild(el("div", "st-note",
      "Keys are stored in your OS keychain when available, otherwise a gitignored " +
      "file. They are sent only to the local daemon and never displayed back."));
  }

  function boot() {
    var btn = document.getElementById("settingsBtn");
    if (btn) btn.onclick = open;
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  window.PraxisSettings = { open: open };
})();
