/* Home presence, inline approvals, first-win, auth gate, deep-link approve */
(function () {
  "use strict";

  var state = "idle"; // idle | thinking | waiting | running
  var lastDetail = "";

  function $(id) { return document.getElementById(id); }

  function ensureStrip() {
    if ($("presenceStrip")) return;
    var ban = $("healthBanner");
    var strip = document.createElement("div");
    strip.id = "presenceStrip";
    strip.innerHTML =
      '<span class="ps-state"><span class="ps-dot" id="psDot"></span>' +
      '<span id="psLabel">Idle</span></span>' +
      '<span class="ps-meta" id="psMeta"></span>' +
      '<span class="ps-actions">' +
      '<button type="button" id="psPulse">Pulse digest</button>' +
      '<button type="button" class="primary" id="psWin">First win</button>' +
      "</span>";
    if (ban && ban.parentNode) {
      ban.parentNode.insertBefore(strip, ban.nextSibling);
    } else {
      document.body.insertBefore(strip, document.body.firstChild);
    }
    var pulse = $("psPulse");
    if (pulse) pulse.onclick = function () {
      fetch("/api/pulse", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (window.showToast) window.showToast(d.delivered ? "Pulse sent" : "Pulse ready (local)");
          if (d.text && window.appendAgent) window.appendAgent(d.text, "pulse");
        })
        .catch(function () {});
    };
    var win = $("psWin");
    if (win) win.onclick = function () { openFirstWin(true); };
  }

  function setPresence(s, detail) {
    state = s || "idle";
    lastDetail = detail || "";
    ensureStrip();
    var dot = $("psDot");
    var lab = $("psLabel");
    var meta = $("psMeta");
    if (!dot || !lab) return;
    dot.className = "ps-dot" + (state !== "idle" ? " " + state : "");
    var labels = {
      idle: "Idle",
      thinking: "Thinking",
      waiting: "Waiting on you",
      running: "Running"
    };
    lab.textContent = labels[state] || state;
    if (meta) meta.textContent = lastDetail || "";
  }

  window.PraxisPresence = {
    set: setPresence,
    thinking: function (d) { setPresence("thinking", d || "Working…"); },
    waiting: function (d) { setPresence("waiting", d || "Approval needed"); },
    running: function (d) { setPresence("running", d || "Job in progress"); },
    idle: function (d) { setPresence("idle", d || ""); }
  };

  /* ---------- Inline approval cards ---------- */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function mountInlineApproval(ev) {
    var msgs = $("messages");
    if (!msgs) return;
    var wrap = msgs.querySelector(".msg.agent:last-child .bubble-wrap");
    if (!wrap) {
      // Create a temporary agent row
      var row = document.createElement("div");
      row.className = "msg agent";
      row.innerHTML = '<div class="avatar">P</div><div class="bubble-wrap"></div>';
      msgs.appendChild(row);
      wrap = row.querySelector(".bubble-wrap");
    }
    var card = document.createElement("div");
    card.className = "inline-appr";
    card.dataset.aid = ev.approval_id || "";
    card.innerHTML =
      '<div class="ia-title">⏸ Held: ' + esc(ev.tool || "action") +
      ' <span class="rk">' + esc(ev.risk || "") + "</span></div>" +
      '<div class="ia-preview">' + esc(ev.preview || "Consequential action needs your approval.") + "</div>" +
      '<div class="ia-actions">' +
      '<button type="button" class="primary" data-m="once">Approve once</button>' +
      '<button type="button" class="primary" data-m="chat">This chat</button>' +
      '<button type="button" data-m="always">Always this tool</button>' +
      '<button type="button" class="deny" data-m="deny">Deny</button>' +
      "</div>";
    card.querySelectorAll("button").forEach(function (btn) {
      btn.onclick = function () {
        var m = btn.getAttribute("data-m");
        var id = card.dataset.aid;
        if (!id) return;
        if (m === "deny") {
          if (typeof window.denyApproval === "function") window.denyApproval(id);
        } else if (typeof window.approve === "function") {
          window.approve(id, m);
        }
        card.style.opacity = "0.55";
      };
    });
    wrap.appendChild(card);
    setPresence("waiting", (ev.tool || "action") + " needs approval");
    if (msgs.scrollTop != null) msgs.scrollTop = msgs.scrollHeight;
  }

  window.PraxisInlineApproval = { mount: mountInlineApproval };

  /* Hook agent approval events if dashboard uses global helpers */
  var _origSetCard = null;

  /* ---------- First-win wizard ---------- */
  function firstWinDone() {
    try {
      return localStorage.getItem("praxis.firstwin.v1") === "1";
    } catch (_) { return false; }
  }
  function markFirstWin() {
    try { localStorage.setItem("praxis.firstwin.v1", "1"); } catch (_) {}
    fetch("/api/persona", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ first_win_complete: true })
    }).catch(function () {});
  }

  function openFirstWin(force) {
    if (!force && firstWinDone()) return;
    var ov = $("fwOverlay");
    if (!ov) {
      ov = document.createElement("div");
      ov.id = "fwOverlay";
      ov.className = "fw-overlay";
      ov.innerHTML =
        '<div class="fw-box" role="dialog" aria-labelledby="fwTitle">' +
        '<h2 id="fwTitle">Your first win with Praxis</h2>' +
        '<div class="fw-sub">Three minutes. One governed outcome. Leave mode on <b>Auto</b>.</div>' +
        '<div class="fw-steps" id="fwSteps"></div>' +
        '<div class="fw-actions">' +
        '<button type="button" id="fwSkip">Skip for now</button>' +
        '<button type="button" class="primary" id="fwGo">Start mission 1</button>' +
        "</div></div>";
      document.body.appendChild(ov);
      ov.addEventListener("click", function (e) {
        if (e.target === ov) ov.classList.remove("show");
      });
    }
    var steps = ov.querySelector("#fwSteps");
    var missions = (window.PraxisIntent && window.PraxisIntent.missions) || [];
    var tour = (window.PraxisFriendly && window.PraxisFriendly.loadTour)
      ? window.PraxisFriendly.loadTour() : { done: {} };
    steps.innerHTML = "";
    missions.slice(0, 3).forEach(function (m, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "fw-step" + (tour.done && tour.done[m.id] ? " done" : "");
      b.innerHTML =
        '<span class="n">' + (tour.done && tour.done[m.id] ? "✓" : String(i + 1)) + "</span>" +
        "<div><b>" + esc(m.title) + "</b><div style='color:var(--muted);font-size:.78rem;margin-top:.15rem'>" +
        esc(m.desc) + "</div></div>";
      b.onclick = function () {
        ov.classList.remove("show");
        if (typeof window.setMode === "function") window.setMode(m.mode === "auto" ? "auto" : m.mode);
        var ta = $("message");
        if (ta) {
          ta.value = m.prompt;
          ta.focus();
          if (typeof window.autoGrow === "function") window.autoGrow(ta);
        }
      };
      steps.appendChild(b);
    });
    var skip = ov.querySelector("#fwSkip");
    var go = ov.querySelector("#fwGo");
    if (skip) skip.onclick = function () {
      markFirstWin();
      ov.classList.remove("show");
    };
    if (go) go.onclick = function () {
      var first = missions[0];
      ov.classList.remove("show");
      if (first && typeof window.setMode === "function") {
        window.setMode(first.mode === "auto" ? "auto" : first.mode);
        var ta = $("message");
        if (ta) {
          ta.value = first.prompt;
          ta.focus();
        }
      }
    };
    ov.classList.add("show");
  }

  window.PraxisFirstWin = { open: openFirstWin, mark: markFirstWin, done: firstWinDone };

  /* ---------- Auth gate ---------- */
  var _token = "";
  try { _token = sessionStorage.getItem("praxis.auth.token") || ""; } catch (_) {}

  function authHeaders(h) {
    h = h || {};
    if (_token) {
      h["Authorization"] = "Bearer " + _token;
      h["X-Praxis-Token"] = _token;
    }
    return h;
  }

  // Patch fetch for same-origin API calls
  var _fetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var url = typeof input === "string" ? input : (input && input.url) || "";
    if (url && url.charAt(0) === "/" && _token) {
      init.headers = authHeaders(Object.assign({}, init.headers || {}));
    }
    return _fetch.call(this, input, init).then(function (resp) {
      if (resp.status === 401 && window.PraxisAuth) {
        window.PraxisAuth.prompt();
      }
      return resp;
    });
  };

  function showAuth(required) {
    var ov = $("authOverlay");
    if (!ov) {
      ov = document.createElement("div");
      ov.id = "authOverlay";
      ov.className = "auth-overlay";
      ov.innerHTML =
        '<div class="auth-box">' +
        "<h2>Praxis session</h2>" +
        "<p>This daemon requires a shared token for non-local access. Paste the token from " +
        "<code>PRAXIS_AUTH_TOKEN</code> or Settings.</p>" +
        '<input id="authToken" type="password" placeholder="Session token" autocomplete="off" />' +
        '<button type="button" id="authGo">Unlock</button>' +
        "</div>";
      document.body.appendChild(ov);
      ov.querySelector("#authGo").onclick = function () {
        var v = (ov.querySelector("#authToken").value || "").trim();
        if (!v) return;
        _token = v;
        try { sessionStorage.setItem("praxis.auth.token", v); } catch (_) {}
        ov.classList.remove("show");
        if (typeof window.refresh === "function") window.refresh();
      };
    }
    if (required) ov.classList.add("show");
  }

  window.PraxisAuth = {
    prompt: function () { showAuth(true); },
    token: function () { return _token; },
    setToken: function (t) {
      _token = t || "";
      try { sessionStorage.setItem("praxis.auth.token", _token); } catch (_) {}
    },
    check: function () {
      return fetch("/api/auth/status")
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (s.required && !_token) showAuth(true);
          return s;
        })
        .catch(function () { return {}; });
    }
  };

  /* ---------- Deep-link ?approve= ---------- */
  function handleDeepLink() {
    try {
      var q = new URLSearchParams(location.search);
      var aid = q.get("approve");
      var deny = q.get("deny");
      if (aid && typeof window.approve === "function") {
        setTimeout(function () { window.approve(aid, "once"); }, 600);
        history.replaceState({}, "", location.pathname);
      } else if (deny && typeof window.denyApproval === "function") {
        setTimeout(function () { window.denyApproval(deny); }, 600);
        history.replaceState({}, "", location.pathname);
      }
    } catch (_) {}
  }

  function boot() {
    ensureStrip();
    setPresence("idle", "");
    window.PraxisAuth.check();
    handleDeepLink();
    // First-win after short delay if never completed
    setTimeout(function () {
      if (!firstWinDone()) openFirstWin(false);
    }, 900);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
