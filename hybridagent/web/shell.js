/* Command Deck shell — tabbed right rail. Served from /web/shell.js.
 * Keeps all existing mount IDs; only re-parents visibility via tabs.
 */
(function () {
  "use strict";

  var KEY = "praxis.deck.rail.tab.v1";
  var TABS = ["ops", "work", "mind", "more"];

  function $(id) { return document.getElementById(id); }

  function setTab(name) {
    if (TABS.indexOf(name) < 0) name = "ops";
    try { localStorage.setItem(KEY, name); } catch (_) {}
    var tabs = document.querySelectorAll(".rail-tabs [data-rail]");
    for (var i = 0; i < tabs.length; i++) {
      var on = tabs[i].getAttribute("data-rail") === name;
      tabs[i].classList.toggle("active", on);
      tabs[i].setAttribute("aria-selected", on ? "true" : "false");
    }
    var panes = document.querySelectorAll(".rail-pane[data-pane]");
    for (var j = 0; j < panes.length; j++) {
      panes[j].classList.toggle("active", panes[j].getAttribute("data-pane") === name);
    }
  }

  function loadTab() {
    try {
      var t = localStorage.getItem(KEY);
      if (t && TABS.indexOf(t) >= 0) return t;
    } catch (_) {}
    return "ops";
  }

  /** Bump a tab badge when its domain has something to see (e.g. tasks). */
  function signal(tab, on) {
    var btn = document.querySelector('.rail-tabs [data-rail="' + tab + '"]');
    if (!btn) return;
    btn.classList.toggle("has-signal", !!on);
  }

  window.PraxisShell = {
    setTab: setTab,
    signal: signal,
    /** Jump to Approvals (primary strip) and flash ops if needed */
    focusApprovals: function () {
      var el = $("approvals");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  };

  function boot() {
    document.body.classList.add("deck-shell");
    var bar = document.querySelector(".rail-tabs");
    if (bar) {
      bar.addEventListener("click", function (e) {
        var b = e.target.closest("[data-rail]");
        if (!b) return;
        setTab(b.getAttribute("data-rail"));
      });
    }
    setTab(loadTab());

    // When header approval badge is clicked, ensure primary strip is visible
    var badge = $("apprBadge");
    if (badge) {
      badge.addEventListener("click", function () {
        window.PraxisShell.focusApprovals();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
