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

    // ---- Telegram one-click enable ----
    box.appendChild(el("div", "st-section", "Telegram (inbound)"));
    var tgBox = el("div", "st-tg");
    tgBox.id = "stTg";
    tgBox.innerHTML = '<div class="empty">Loading Telegram status…</div>';
    box.appendChild(tgBox);
    loadTelegram(tgBox);
  }

  function loadTelegram(tgBox) {
    if (!tgBox) tgBox = document.getElementById("stTg");
    if (!tgBox) return;
    api("/api/channels/telegram").then(function (st) {
      paintTelegram(tgBox, st || {});
    }).catch(function () {
      tgBox.innerHTML = '<div class="empty">Could not load Telegram status.</div>';
    });
  }

  function paintTelegram(tgBox, st) {
    var live = st.enabled && st.has_token;
    var probe = st.probe || {};
    var statusLine = live
      ? ('<span class="st-tg-ok">● Live</span> ' +
         (probe.username ? ('@' + esc(probe.username)) : 'polling for messages'))
      : (st.configured
        ? '<span class="st-tg-off">○ Configured, disabled</span>'
        : '<span class="st-tg-off">○ Not configured</span>');
    tgBox.innerHTML = "";
    tgBox.appendChild(el("div", "st-tg-status", statusLine));
    tgBox.appendChild(el("div", "st-note", esc(st.hint || "")));

    var form = el("div", "st-form st-tg-form");
    var tok = el("input", "st-input");
    tok.type = "password";
    tok.placeholder = st.token_is_env_ref
      ? "Using ${TELEGRAM_BOT_TOKEN} — paste to override"
      : "Bot token from @BotFather";
    tok.id = "stTgToken";
    var chat = el("input", "st-input");
    chat.type = "text";
    chat.placeholder = "Your chat id (message the bot, then getUpdates)";
    chat.id = "stTgChat";
    chat.value = st.chat_id || "";
    var envRef = el("label", "st-tg-check");
    envRef.innerHTML =
      '<input type="checkbox" id="stTgEnv"' +
      (st.token_is_env_ref ? " checked" : "") +
      '> Store as <code>${TELEGRAM_BOT_TOKEN}</code> (recommended)';
    form.appendChild(tok);
    form.appendChild(chat);
    form.appendChild(envRef);
    tgBox.appendChild(form);

    var actions = el("div", "st-tg-actions");
    var enable = el("button", "st-btn primary", live ? "Save & keep enabled" : "Enable Telegram");
    enable.type = "button";
    enable.onclick = function () {
      var body = {
        action: "configure",
        enabled: true,
        bot_token: (document.getElementById("stTgToken") || {}).value || "",
        chat_id: (document.getElementById("stTgChat") || {}).value || "",
        use_env_ref: !!(document.getElementById("stTgEnv") || {}).checked
      };
      // If env-ref checked and field empty, still configure env ref.
      if (body.use_env_ref && !body.bot_token) body.bot_token = "ENV";
      api("/api/channels/telegram", body).then(function (r) {
        if (r.error) { toast(r.error); return; }
        var p = r.probe || {};
        if (p.ok === false && r.has_token) {
          toast("Saved, but probe failed: " + (p.error || "check token"));
        } else if (p.username) {
          toast("Telegram live as @" + p.username);
        } else {
          toast(r.enabled ? "Telegram enabled" : "Saved");
        }
        paintTelegram(tgBox, r);
      });
    };
    var disable = el("button", "st-btn", "Disable");
    disable.type = "button";
    disable.onclick = function () {
      api("/api/channels/telegram", { action: "disable" }).then(function (r) {
        toast("Telegram disabled");
        paintTelegram(tgBox, r);
      });
    };
    actions.appendChild(enable);
    if (st.configured) actions.appendChild(disable);
    tgBox.appendChild(actions);
    tgBox.appendChild(el("div", "st-note",
      "How: create a bot with @BotFather → paste token → message the bot → set chat id " +
      "(or leave blank to accept any). The daemon polls getUpdates while running. " +
      "Reply <code>approve &lt;id&gt;</code> to held actions."));
  }

  function boot() {
    var btn = document.getElementById("settingsBtn");
    if (btn) btn.onclick = open;
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  window.PraxisSettings = { open: open };
})();
