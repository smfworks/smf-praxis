/* Praxis Friendliness Sprint A — intent routing, missions, health banner,
 * outcome cards, keyboard approval shortcuts. Served from /web/friendliness.js.
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
      title: "Summarize a URL",
      desc: "Paste any link — Praxis researches and cites sources.",
      prompt: "Summarize https://example.com for me in 5 bullets with sources.",
      tag: "research",
      mode: "auto"
    },
    {
      title: "Draft an email (held)",
      desc: "Write a professional note. Sending stays behind your approval.",
      prompt: "Draft a short follow-up email to Alex thanking them for the meeting and proposing next Tuesday.",
      tag: "safe send",
      mode: "chat"
    },
    {
      title: "Ask your knowledge base",
      desc: "Grounded Q&A over notes Praxis already knows.",
      prompt: "According to my knowledge base, what is Praxis and how does governance work?",
      tag: "look up",
      mode: "ask"
    },
    {
      title: "Queue background work",
      desc: "Hand off a goal to the autonomous task queue.",
      prompt: "Queue a task: scan for urgent follow-ups and draft a short status note.",
      tag: "work on this",
      mode: "do"
    }
  ];

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
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

  /* ---------- Outcome card helper ---------- */
  window.PraxisOutcome = {
    renderHtml: function (o) {
      o = o || {};
      var st = o.status || "done";
      var cls = "info";
      if (st === "completed" || st === "done" || st === "ok") cls = "ok";
      else if (st === "waiting_approval" || st === "held" || st === "warn") cls = "warn";
      else if (st === "failed" || st === "error" || st === "bad") cls = "bad";
      var rows = "";
      if (o.goal) rows += '<div class="oc-row"><b>Goal</b><span>' + esc(o.goal) + "</span></div>";
      if (o.task_id) rows += '<div class="oc-row"><b>Task</b><span>' + esc(o.task_id) + "</span></div>";
      if (o.cost != null) rows += '<div class="oc-row"><b>Cost</b><span>' + esc(String(o.cost)) + "</span></div>";
      if (o.next) rows += '<div class="oc-row"><b>Next</b><span>' + esc(o.next) + "</span></div>";
      var body = o.output ? '<div class="oc-body">' + esc(o.output).slice(0, 1200) + "</div>" : "";
      return (
        '<div class="outcome">' +
        '<div class="oc-head"><span class="pill-st ' + cls + '">' + esc(st) + "</span>" +
        esc(o.title || "Outcome") + "</div>" +
        rows + body + "</div>"
      );
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
          var friendly = last;
          if (/overloaded|503/i.test(last)) {
            friendly = "The model provider is overloaded. Retry shortly, or switch models in the Model panel.";
          } else if (/timed out|timeout|remote end closed/i.test(last)) {
            friendly = "The model provider timed out. Praxis backed off idle heartbeats; chat should still work — retry if a turn failed.";
          } else if (/api key|401|403|missing api key/i.test(last)) {
            friendly = "Provider authentication failed. Check your API key in Settings / Set up Praxis.";
          }
          setBanner(true, friendly, "bad", "Open model panel", function () {
            var p = document.getElementById("prov");
            if (p) p.focus();
          });
          return;
        }
        setBanner(false);
      } catch (_) { /* ignore */ }
    }
  };

  /* ---------- Missions on welcome ---------- */
  function paintMissions() {
    var welcome = document.querySelector("#messages .welcome");
    if (!welcome || welcome.querySelector(".missions")) return;
    var chips = welcome.querySelector(".chips");
    if (chips) chips.remove();
    var grid = document.createElement("div");
    grid.className = "missions";
    MISSIONS.forEach(function (m) {
      var card = document.createElement("button");
      card.type = "button";
      card.className = "mission";
      card.innerHTML =
        '<div class="m-title">' + esc(m.title) + "</div>" +
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
    var tip = document.createElement("p");
    tip.style.cssText = "margin-top:1rem;font-size:.78rem;color:var(--faint)";
    tip.textContent = "Tip: leave mode on Auto — Praxis picks Look up / Research / Work on this when your message matches.";
    welcome.appendChild(tip);
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
    // Re-paint missions when welcome is recreated
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
