/* Praxis Friendliness Sprint B — Auto routing, missions, health banner,
 * outcome cards, guided first-run, friendly errors, approval "what next".
 * Served from /web/friendliness.js.
 */
(function () {
  "use strict";

  var URL_RE = /https?:\/\/\S+/i;
  var RESEARCH_RE = /\b(research|look up|search (the )?(web|internet)|find (articles|sources)|latest (news|on)|summarize (this|the) (url|page|link|article))\b/i;
  var ASK_RE = /\b(according to (my|the) (kb|knowledge|notes|wiki|memory)|from (my|the) (knowledge|notes|wiki|docs)|cite (sources|your sources)|grounded|in (the )?knowledge base)\b/i;
  var DO_RE = /\b(queue (this|a )?task|run this (as )?(a )?(background|autonomous)|work on this (in the )?background|schedule this|add to (the )?board|every (day|morning|evening|hour)|daily at|cron)\b/i;
  var AGENT_RE = /\b(use tools|browse|open (the )?browser|click|fill (the )?form|send (the )?email|delete|call (the )?agent|delegate)\b/i;

  var LABELS = {
    auto: "Auto",
    chat: "Chat",
    ask: "Look up",
    research: "Research",
    do: "Work on this",
    agent: "Tools"
  };

  var TOUR_KEY = "praxis.friendly.tour.v1";

  function detectIntent(text) {
    var t = (text || "").trim();
    if (!t) return "chat";
    if (DO_RE.test(t)) return "do";
    if (ASK_RE.test(t)) return "ask";
    if (URL_RE.test(t) || RESEARCH_RE.test(t)) return "research";
    if (AGENT_RE.test(t)) return "agent";
    return "chat";
  }

  var MISSIONS = [
    {
      id: "research",
      title: "1 · Research → brief",
      desc: "Web research with citations — the primary look-up job.",
      prompt: "Research the latest open-source agent runtimes and summarize in 5 bullets with sources.",
      tag: "job: research",
      mode: "auto"
    },
    {
      id: "hold",
      title: "2 · Draft behind approval",
      desc: "Write a professional draft. Send stays held for you.",
      prompt: "Draft a short follow-up email to Alex thanking them for the meeting and proposing next Tuesday. Do not send it.",
      tag: "job: draft",
      mode: "chat"
    },
    {
      id: "do",
      title: "3 · Scheduled colleague",
      desc: "Queue a goal the daemon can also run on a cron schedule.",
      prompt: "Queue a task: scan for urgent follow-ups and draft a short status note (do not send).",
      tag: "job: schedule",
      mode: "do"
    },
    {
      id: "ask",
      title: "Bonus · Ask your knowledge base",
      desc: "Grounded Q&A over notes Praxis already knows.",
      prompt: "According to my knowledge base, what is Praxis and how does governance work?",
      tag: "look up",
      mode: "ask"
    }
  ];

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function loadTour() {
    try {
      var raw = localStorage.getItem(TOUR_KEY);
      var o = raw ? JSON.parse(raw) : null;
      if (!o || typeof o !== "object") return { done: {} };
      if (!o.done || typeof o.done !== "object") o.done = {};
      return o;
    } catch (_) {
      return { done: {} };
    }
  }

  function saveTour(o) {
    try { localStorage.setItem(TOUR_KEY, JSON.stringify(o)); } catch (_) {}
  }

  function markTour(step) {
    var o = loadTour();
    o.done[step] = true;
    o.updated = Date.now();
    saveTour(o);
    paintMissions();
    paintTourHint();
  }

  /* ---------- Intent API for dashboard ---------- */
  window.PraxisIntent = {
    detect: detectIntent,
    labels: LABELS,
    missions: MISSIONS,
    resolveMode: function (uiMode, text) {
      if (!uiMode || uiMode === "auto") return detectIntent(text);
      return uiMode;
    }
  };

  /* ---------- Friendly error copy ---------- */
  function friendlyError(err) {
    var s = String(err == null ? "" : (err.message || err));
    if (!s) return "Something went wrong. Retry in a moment.";
    if (/overloaded|503/i.test(s)) {
      return "The model provider is overloaded. Wait a few seconds, or switch models in the Model panel.";
    }
    if (/timed out|timeout|remote end closed|Read timed out/i.test(s)) {
      return "The model provider timed out. Retry the message — idle heartbeats are already backed off.";
    }
    if (/api key|401|403|missing api key|Unauthorized/i.test(s)) {
      return "Provider authentication failed. Open Set up Praxis or Settings and check your API key.";
    }
    if (/Failed to fetch|NetworkError|ECONNREFUSED|connection refused/i.test(s)) {
      return "Could not reach the Praxis daemon or provider. Confirm the daemon is running on this machine.";
    }
    if (/HTTP 5\d\d/i.test(s)) {
      return "The provider returned a server error. Retry shortly or switch models.";
    }
    // Strip long stack / URL noise for chat display
    s = s.replace(/\s+at\s+\S+.*/g, "").replace(/\n+/g, " ").trim();
    if (s.length > 220) s = s.slice(0, 217) + "…";
    if (/^Error:\s*/i.test(s)) return s;
    return "Error: " + s;
  }

  window.PraxisFriendly = {
    error: friendlyError,
    markTour: markTour,
    loadTour: loadTour
  };

  /* ---------- Outcome card helper ---------- */
  window.PraxisOutcome = {
    renderHtml: function (o) {
      o = o || {};
      var st = o.status || "done";
      var cls = "info";
      if (st === "completed" || st === "done" || st === "ok" || st === "answered") cls = "ok";
      else if (st === "waiting_approval" || st === "held" || st === "warn" || st === "pending") cls = "warn";
      else if (st === "failed" || st === "error" || st === "bad") cls = "bad";
      var rows = "";
      if (o.goal) rows += '<div class="oc-row"><b>Goal</b><span>' + esc(o.goal) + "</span></div>";
      if (o.task_id) rows += '<div class="oc-row"><b>Task</b><span>' + esc(o.task_id) + "</span></div>";
      if (o.mode) rows += '<div class="oc-row"><b>Mode</b><span>' + esc(o.mode) + "</span></div>";
      if (o.citations != null && o.citations !== "") {
        rows += '<div class="oc-row"><b>Sources</b><span>' + esc(String(o.citations)) + "</span></div>";
      }
      if (o.cost != null) rows += '<div class="oc-row"><b>Cost</b><span>' + esc(String(o.cost)) + "</span></div>";
      if (o.ran) rows += '<div class="oc-row"><b>Ran</b><span>' + esc(o.ran) + "</span></div>";
      if (o.changed) rows += '<div class="oc-row"><b>Changed</b><span>' + esc(o.changed) + "</span></div>";
      if (o.next) rows += '<div class="oc-row"><b>Next</b><span>' + esc(o.next) + "</span></div>";
      var body = o.output ? '<div class="oc-body">' + esc(o.output).slice(0, 1200) + "</div>" : "";
      return (
        '<div class="outcome">' +
        '<div class="oc-head"><span class="pill-st ' + cls + '">' + esc(st) + "</span>" +
        esc(o.title || "Outcome") + "</div>" +
        rows + body + "</div>"
      );
    },
    attach: function (opts) {
      var msgs = document.getElementById("messages");
      if (!msgs) return null;
      var wrap = msgs.querySelector(".msg.agent:last-child .bubble-wrap");
      if (!wrap) return null;
      var holder = document.createElement("div");
      holder.innerHTML = window.PraxisOutcome.renderHtml(opts || {});
      if (holder.firstChild) {
        wrap.appendChild(holder.firstChild);
        return holder.firstChild;
      }
      return null;
    }
  };

  /* ---------- Health banner ---------- */
  function setBanner(show, msg, kind, actionLabel, actionFn) {
    var el = document.getElementById("healthBanner");
    if (!el) return;
    if (!show) {
      el.className = "";
      el.classList.remove("show");
      el.innerHTML = "";
      return;
    }
    el.className = "show " + (kind || "warn");
    el.innerHTML =
      '<div class="hb-msg">' + esc(msg) + "</div>" +
      (actionLabel
        ? '<button type="button" class="hb-act" id="hbAct">' + esc(actionLabel) + "</button>"
        : "");
    var b = document.getElementById("hbAct");
    if (b && actionFn) b.onclick = actionFn;
  }

  window.PraxisHealth = {
    set: setBanner,
    refresh: async function () {
      try {
        var m = await fetch("/api/model").then(function (r) { return r.json(); }).catch(function () { return {}; });
        var st = await fetch("/status").then(function (r) { return r.json(); }).catch(function () { return {}; });
        var errs = (st.state && st.state.errors) || [];
        if (!m.configured) {
          setBanner(
            true,
            "You're on the offline mock model — answers are simulated until you connect a live provider.",
            "warn",
            "Set up Praxis",
            function () { if (window.PraxisOnboard) window.PraxisOnboard.open(); }
          );
          return;
        }
        var model = String(m.model || "");
        if (/mock/i.test(model) && m.configured !== true) {
          setBanner(true, "Offline mock model is active.", "warn", "Set up", function () {
            if (window.PraxisOnboard) window.PraxisOnboard.open();
          });
          return;
        }
        if (errs.length) {
          var last = String(errs[errs.length - 1] || "");
          setBanner(true, friendlyError(last), "bad", "Open model panel", function () {
            var p = document.getElementById("prov");
            if (p) p.focus();
          });
          return;
        }
        setBanner(false);
      } catch (_) { /* ignore */ }
    }
  };

  /* ---------- Missions + guided tour ---------- */
  function paintTourHint() {
    var welcome = document.querySelector("#messages .welcome");
    if (!welcome) return;
    var existing = welcome.querySelector(".tour-hint");
    if (existing) existing.remove();
    var tour = loadTour();
    var done = tour.done || {};
    var n = ["research", "hold", "ask", "do"].filter(function (k) { return done[k]; }).length;
    var hint = document.createElement("div");
    hint.className = "tour-hint";
    if (n >= 4) {
      hint.innerHTML = "<b>Tour complete.</b> Leave mode on Auto — Praxis picks the right path from your words.";
    } else if (n === 0) {
      hint.innerHTML = "<b>First five minutes:</b> start with <em>Summarize a URL</em>, then try a held draft so you see approvals.";
    } else {
      hint.innerHTML = "<b>Tour progress:</b> " + n + "/4 missions tried. Next: pick an unchecked card below.";
    }
    welcome.appendChild(hint);
  }

  function paintMissions() {
    var welcome = document.querySelector("#messages .welcome");
    if (!welcome) return;
    var old = welcome.querySelector(".missions");
    if (old) old.remove();
    var chips = welcome.querySelector(".chips");
    if (chips) chips.remove();
    var tip = welcome.querySelector(".missions-tip");
    if (tip) tip.remove();

    var tour = loadTour();
    var done = tour.done || {};
    var grid = document.createElement("div");
    grid.className = "missions";
    MISSIONS.forEach(function (m) {
      var card = document.createElement("button");
      card.type = "button";
      card.className = "mission" + (done[m.id] ? " done" : "");
      card.innerHTML =
        '<div class="m-title">' + esc(m.title) +
        (done[m.id] ? ' <span class="m-check">✓</span>' : "") + "</div>" +
        '<p class="m-desc">' + esc(m.desc) + "</p>" +
        '<span class="m-tag">' + esc(m.tag) + "</span>";
      card.onclick = function () {
        if (typeof window.setMode === "function") {
          window.setMode(m.mode === "auto" ? "auto" : m.mode);
        }
        var ta = document.getElementById("message");
        if (ta) {
          ta.value = m.prompt;
          ta.focus();
          if (typeof window.autoGrow === "function") window.autoGrow(ta);
          if (typeof window.updateIntentChip === "function") window.updateIntentChip();
        }
      };
      grid.appendChild(card);
    });
    welcome.appendChild(grid);
    var tipEl = document.createElement("p");
    tipEl.className = "missions-tip";
    tipEl.style.cssText = "margin-top:1rem;font-size:.78rem;color:var(--faint)";
    tipEl.textContent = "Tip: leave mode on Auto — Praxis picks Look up / Research / Work on this when your message matches.";
    welcome.appendChild(tipEl);
    paintTourHint();
  }

  /* ---------- Keyboard shortcuts for approvals ---------- */
  document.addEventListener("keydown", function (e) {
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/i.test(e.target.tagName)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var list = document.querySelectorAll("#approvals .approval");
    if (!list.length) return;
    var first = list[0];
    var key = e.key.toLowerCase();
    if (key === "a") {
      e.preventDefault();
      var once = first.querySelector("button.once");
      if (once) once.click();
    } else if (key === "c") {
      e.preventDefault();
      var chat = first.querySelector("button.chat");
      if (chat) chat.click();
    } else if (key === "d") {
      e.preventDefault();
      var deny = first.querySelector("button.deny");
      if (deny) deny.click();
    }
  });

  function boot() {
    paintMissions();
    var msgs = document.getElementById("messages");
    if (msgs && window.MutationObserver) {
      var mo = new MutationObserver(function () { paintMissions(); });
      mo.observe(msgs, { childList: true });
    }
    window.PraxisHealth.refresh();
    setInterval(function () { window.PraxisHealth.refresh(); }, 12000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
