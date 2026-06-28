/* Praxis setup wizard — pick a provider, model, and key right in the dashboard
 * (no CLI). Auto-opens on first run; also opened from the first-run CTA and the
 * command palette. Served from /web/onboard.js. */
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
  function toast(m) { if (window.showToast) window.showToast(m); }

  var overlay = null, providers = [], sel = null;

  async function ensureProviders() {
    if (!providers.length) {
      try { providers = await api("/api/providers"); } catch (_) { providers = []; }
    }
  }
  function current() {
    return providers.find(function (p) { return p.id === (sel && sel.value); }) || providers[0] || {};
  }

  function ensure() {
    if (overlay) return;
    overlay = el("div", "ob-overlay");
    overlay.appendChild(el("div", "ob-box"));
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && overlay.classList.contains("show")) close();
    });
    document.body.appendChild(overlay);
  }
  async function open() { await ensureProviders(); ensure(); render(); overlay.classList.add("show"); }
  function close() { if (overlay) overlay.classList.remove("show"); }
  function skip() { try { sessionStorage.setItem("praxisOnboardDismissed", "1"); } catch (_) { } close(); }

  function render() {
    var chosen = sel ? sel.value : "";
    var box = overlay.querySelector(".ob-box");
    box.innerHTML = "";
    box.appendChild(el("div", "ob-head",
      "<div><b>Set up Praxis</b></div><div class='ob-sub'>Connect a model to go " +
      "from the offline mock to a live colleague.</div>"));

    box.appendChild(el("label", "ob-label", "Provider"));
    sel = el("select", "ob-sel");
    providers.forEach(function (p) { var o = el("option"); o.value = p.id; o.textContent = p.label; sel.appendChild(o); });
    if (chosen) sel.value = chosen;     // preserve the selection across re-render
    sel.onchange = render;
    box.appendChild(sel);

    var prov = current();

    box.appendChild(el("label", "ob-label", "Model"));
    var input = el("input", "ob-input"); input.id = "obModel"; input.setAttribute("list", "obModels");
    input.value = (prov.models && prov.models[0]) || "";
    var dl = el("datalist"); dl.id = "obModels";
    (prov.models || []).forEach(function (m) { var o = el("option"); o.value = m; dl.appendChild(o); });
    box.appendChild(input); box.appendChild(dl);
    if (prov.notes) box.appendChild(el("div", "ob-note", esc(prov.notes)));

    box.appendChild(el("label", "ob-label", "API key"));
    if (!prov.needs_key) {
      box.appendChild(el("div", "ob-note", "No API key required for this provider."));
    } else {
      var keyBox = el("div", "ob-key");
      keyBox.innerHTML =
        '<label class="ob-radio"><input type="radio" name="obkey" value="env" checked> Use environment variable <code>' +
        esc(prov.key_env || "") + '</code> <span class="ob-rec">recommended</span></label>' +
        '<label class="ob-radio"><input type="radio" name="obkey" value="paste"> Paste a key now ' +
        '<span class="ob-rec">stored in your OS keychain</span></label>' +
        '<input type="password" class="ob-input ob-pw" id="obKey" placeholder="Paste ' +
        esc(prov.key_env || "API key") + '" hidden>';
      box.appendChild(keyBox);
      var pw = keyBox.querySelector("#obKey");
      keyBox.querySelectorAll('input[name="obkey"]').forEach(function (r) {
        r.onchange = function () { pw.hidden = !(r.value === "paste" && r.checked); if (!pw.hidden) pw.focus(); };
      });
    }

    var actions = el("div", "ob-actions");
    var finishBtn = el("button", "ob-btn primary", "Finish setup"); finishBtn.type = "button"; finishBtn.onclick = finish;
    var skipBtn = el("button", "ob-btn ghost", "Skip for now"); skipBtn.type = "button"; skipBtn.onclick = skip;
    actions.appendChild(finishBtn); actions.appendChild(skipBtn);
    box.appendChild(actions);
  }

  async function finish() {
    var prov = current();
    var model = (document.getElementById("obModel").value || "").trim();
    if (!model) { toast("Enter a model."); return; }
    var body = { provider: prov.id, model: model, use_env_ref: true };
    if (prov.needs_key) {
      var paste = overlay.querySelector('input[name="obkey"][value="paste"]');
      if (paste && paste.checked) {
        var key = (document.getElementById("obKey").value || "").trim();
        if (!key) { toast("Paste a key, or choose the environment-variable option."); return; }
        body.use_env_ref = false; body.api_key = key;
      }
    }
    var r = await api("/api/onboard", body);
    if (r.error) { toast(r.error); return; }
    close();
    toast("Setup complete \u2014 " + (r.model || model));
    if (window.loadModel) window.loadModel();
    if (window.loadProviders) window.loadProviders();
    if (window.refresh) window.refresh();
  }

  async function boot() {
    try {
      var m = await api("/api/model");
      if (m && !m.configured && !sessionStorage.getItem("praxisOnboardDismissed")) open();
    } catch (_) { /* ignore */ }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  window.PraxisOnboard = { open: open };
})();
