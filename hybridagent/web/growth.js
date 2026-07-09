/* Skills inbox, evolution proposals, user model, agent rooms */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function api(path, body) {
    var opt = body !== undefined
      ? { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body) }
      : undefined;
    return fetch(path, opt).then(function (r) { return r.json(); });
  }

  function mount(id) {
    var el = document.getElementById(id);
    if (!el) {
      // Create under mind pane if missing
      var mind = document.querySelector('.rail-pane[data-pane="mind"]');
      if (!mind) return null;
      var sec = document.createElement("div");
      sec.className = "rail-section";
      var title = id === "growth-skills" ? "Skills"
        : id === "growth-evolve" ? "Evolution inbox"
        : id === "growth-model" ? "You"
        : id === "growth-rooms" ? "Agent rooms"
        : "Growth";
      sec.innerHTML = "<h2>" + title + '</h2><div id="' + id + '"></div>';
      mind.appendChild(sec);
      el = document.getElementById(id);
    }
    return el;
  }

  function paintModel(data) {
    var el = mount("growth-model");
    if (!el) return;
    var p = (data && data.persona) || {};
    el.innerHTML =
      '<div class="user-model">' +
      '<div class="um-name">' + esc(p.display_name || "You") +
      (p.role ? " · " + esc(p.role) : "") + "</div>" +
      '<div class="um-line">' + esc((data && data.summary) || "No persona yet") + "</div>" +
      (p.tone ? '<div class="um-line">Tone: ' + esc(p.tone) + "</div>" : "") +
      (p.never_do && p.never_do.length
        ? '<div class="um-line">Never: ' + esc(p.never_do.join(", ")) + "</div>"
        : "") +
      '<button type="button" id="umEdit">Edit persona</button>' +
      "</div>";
    var btn = el.querySelector("#umEdit");
    if (btn) btn.onclick = function () {
      if (window.PraxisPersona) window.PraxisPersona.open();
      else if (window.PraxisOnboard) window.PraxisOnboard.open();
    };
  }

  function paintSkills(list) {
    var el = mount("growth-skills");
    if (!el) return;
    if (!list || !list.length) {
      el.innerHTML = '<div class="empty">No skills yet. Complete a multi-step task and accept a skill, or use <code>praxis learn</code>.</div>';
      return;
    }
    el.innerHTML = "";
    list.forEach(function (s) {
      var d = document.createElement("div");
      d.className = "growth-card";
      d.innerHTML =
        "<h4>" + esc(s.name) + (s.enabled === false ? " (off)" : "") + "</h4>" +
        '<div class="g-meta">' + esc(s.trigger || "") + "</div>" +
        '<div class="g-meta">' + esc((s.body_preview || "").slice(0, 160)) + "</div>";
      el.appendChild(d);
    });
  }

  function paintProposals(list) {
    var el = mount("growth-evolve");
    if (!el) return;
    if (!list || !list.length) {
      el.innerHTML =
        '<div class="empty">No evolution proposals. ' +
        '<button type="button" id="evoRun" class="primary" style="margin-top:.4rem">Propose improvements</button></div>';
      var r = el.querySelector("#evoRun");
      if (r) r.onclick = function () { runEvolve(); };
      return;
    }
    el.innerHTML = "";
    var bar = document.createElement("div");
    bar.style.marginBottom = ".4rem";
    bar.innerHTML = '<button type="button" id="evoRun">Refresh proposals</button>';
    el.appendChild(bar);
    bar.querySelector("#evoRun").onclick = function () { runEvolve(); };
    list.forEach(function (p) {
      var d = document.createElement("div");
      d.className = "growth-card";
      d.innerHTML =
        "<h4>" + esc(p.skill_name) + "</h4>" +
        '<div class="g-meta">Fitness ' + esc(String(p.current_fitness)) +
        " → " + esc(String(p.new_fitness)) +
        (p.improves ? " ↑" : "") + "</div>" +
        '<div class="g-meta">' + esc(p.rationale || p.new_trigger || "") + "</div>" +
        (p.diff ? "<pre>" + esc(p.diff).slice(0, 1200) + "</pre>" : "") +
        '<div class="g-actions">' +
        '<button type="button" class="primary" data-a="apply">Apply</button>' +
        '<button type="button" data-a="reject">Reject</button>' +
        "</div>";
      d.querySelector('[data-a="apply"]').onclick = function () {
        api("/api/growth/apply", { id: p.id }).then(function () {
          if (window.showToast) window.showToast("Skill updated: " + p.skill_name);
          refresh();
        });
      };
      d.querySelector('[data-a="reject"]').onclick = function () {
        api("/api/growth/reject", { id: p.id }).then(refresh);
      };
      el.appendChild(d);
    });
  }

  function paintRooms(rooms) {
    var el = mount("growth-rooms");
    if (!el) return;
    el.innerHTML = '<div class="rooms-grid" id="roomsGrid"></div>';
    var grid = el.querySelector("#roomsGrid");
    (rooms || []).forEach(function (r) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "room-chip";
      b.innerHTML = "<b>" + esc(r.name) + "</b><span>" + esc(r.desc || r.role || "") + "</span>";
      b.onclick = function () {
        var ta = document.getElementById("message");
        if (ta) {
          ta.value = "[room:" + r.id + "] ";
          ta.focus();
        }
        if (window.showToast) window.showToast("Room: " + r.name);
      };
      grid.appendChild(b);
    });
  }

  function paintTtft(st) {
    var el = mount("growth-skills");
    if (!el || !st || !st.count) return;
    var pill = document.createElement("div");
    pill.className = "ttft-pill";
    pill.textContent = "Time-to-first-task p50: " +
      (st.p50 != null ? st.p50 + "s" : "—") +
      " (" + st.count + " samples)";
    el.appendChild(pill);
  }

  function runEvolve() {
    if (window.showToast) window.showToast("Evolving skills…");
    api("/api/growth/evolve", { limit: 3 }).then(function (res) {
      paintProposals(res.proposals || []);
      if (window.showToast) {
        window.showToast((res.proposals || []).length + " proposal(s)");
      }
    });
  }

  function paintBrowser(snap) {
    var el = document.getElementById("browser-snap");
    if (!el) return;
    if (!snap || (!snap.url && !snap.title)) {
      el.innerHTML = '<div class="empty">No page loaded. Ask Praxis to browse a URL.</div>';
      return;
    }
    el.innerHTML =
      '<div class="growth-card">' +
      "<h4>" + esc(snap.title || "(no title)") + "</h4>" +
      '<div class="g-meta">' + esc(snap.url || "") + "</div>" +
      '<div class="g-meta">' + esc((snap.text_preview || "").slice(0, 280)) + "</div>" +
      "</div>";
  }

  function refresh() {
    Promise.all([
      api("/api/growth/model").catch(function () { return {}; }),
      api("/api/growth/skills").catch(function () { return { skills: [] }; }),
      api("/api/growth/proposals").catch(function () { return { proposals: [] }; }),
      api("/api/growth/rooms").catch(function () { return { rooms: [] }; }),
      api("/api/growth/ttft").catch(function () { return {}; }),
      api("/api/browser/snapshot").catch(function () { return {}; })
    ]).then(function (xs) {
      paintModel(xs[0]);
      paintSkills((xs[1] && xs[1].skills) || []);
      paintProposals((xs[2] && xs[2].proposals) || []);
      paintRooms((xs[3] && xs[3].rooms) || []);
      paintTtft(xs[4] || {});
      paintBrowser(xs[5] || {});
    });
  }

  /* Persona mini-editor */
  window.PraxisPersona = {
    open: function () {
      var name = prompt("Your name / how Praxis should address you:", "") || "";
      if (name === null) return;
      var role = prompt("Your role (e.g. founder, eng lead):", "") || "";
      var tone = prompt("Preferred tone:", "professional, concise, helpful") || "";
      var never = prompt("Never do (comma-separated):", "") || "";
      api("/api/persona", {
        display_name: name,
        role: role,
        tone: tone,
        never_do: never,
        onboarding_complete: true
      }).then(function () {
        if (window.showToast) window.showToast("Persona saved");
        refresh();
      });
    }
  };

  window.PraxisGrowth = { refresh: refresh, evolve: runEvolve };

  function boot() {
    // Delay so mind pane exists
    setTimeout(refresh, 400);
    // Refresh occasionally
    setInterval(function () {
      if (document.hidden) return;
      var mind = document.querySelector('.rail-pane[data-pane="mind"].active');
      if (mind) refresh();
    }, 45000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
