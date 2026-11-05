/* console.js — live freshness, as progressive enhancement only
   (alpha-engine-config-I12783, plan §3: "panels auto-refresh at their
   source's cadence and show 'updated N min ago'").

   Every page is complete without this file: the server renders each as-of
   with its age at render time, and a reload re-renders it. With script on,
   two things change and nothing else does:

   1. Ages tick. Every `<time datetime>` the server stamped with a `.age`
      span is recomputed each 30 s by the same rules as the server's
      `_age_text` (console/render/html.py), so "7m ago" never sits frozen
      on a page left open.
   2. The page re-reads itself at the index's rebuild cadence (the
      `console-refresh` meta, seconds; nothing on a page can change faster
      than the index it renders from). The fetched page replaces the body;
      the URL, scroll position and open <details> are kept. A status line
      says when the data was built, and a failed refresh says so in words,
      with the time of the data still shown — it never leaves stale data
      looking current (§5.5).

   No network origin but this one, no library, no build step. */
(function () {
  "use strict";

  var TICK_MS = 30000;
  var MIN_REFRESH_S = 60;

  function ageText(seconds) {
    if (seconds < 60) return "under a minute ago";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 48 * 3600) return Math.floor(seconds / 3600) + "h ago";
    return Math.floor(seconds / 86400) + "d ago";
  }

  function ageFrom(stamp, nowMs) {
    var t = Date.parse(stamp);
    if (isNaN(t)) return null;
    var seconds = (nowMs - t) / 1000;
    if (seconds < -60) return ageText(-seconds).replace(/ ago$/, "") + " ahead of now";
    return ageText(Math.max(seconds, 0));
  }

  function utcMinute(ms) {
    return new Date(ms).toISOString().slice(0, 16).replace("T", " ") + "Z";
  }

  function meta(doc, name) {
    var el = doc.querySelector('meta[name="' + name + '"]');
    return el ? el.getAttribute("content") : null;
  }

  // Open <details> survive a refresh by identity, never by position (codex
  // e051f22ff1737637: one new report collapsed every open one, and a shifted
  // list opened the wrong ones). Each element is described as {id, cls,
  // text, open}. A refreshed element takes the state of the old one with the
  // same id, else the same summary text (the nth of that text, so repeated
  // summaries pair in order). A section that is the only one of its class on
  // both pages is the same section even when a count in its summary moved.
  // Anything else is new and keeps the server's state. Returns, per new
  // element, true / false, or null for "leave it as served".
  function carryOpen(before, after) {
    function keyed(list) {
      var seen = Object.create(null), perClass = Object.create(null);
      list.forEach(function (d) { perClass[d.cls || ""] = (perClass[d.cls || ""] || 0) + 1; });
      return list.map(function (d) {
        var key = d.id ? "id " + d.id : "text " + (d.text || "");
        seen[key] = (seen[key] || 0) + 1;
        var only = !d.id && d.cls && perClass[d.cls] === 1 ? d.cls : null;
        return {key: key + " #" + seen[key], only: only, open: !!d.open};
      });
    }
    var byKey = Object.create(null), byClass = Object.create(null);
    keyed(before).forEach(function (o) {
      byKey[o.key] = o;
      if (o.only) byClass[o.only] = o;
    });
    return keyed(after).map(function (d) {
      var hit = byKey[d.key] || (d.only && byClass[d.only]);
      return hit ? hit.open : null;
    });
  }

  // Outside a browser (the console's own tests run this file under node)
  // only the pure functions above are exported; nothing below runs.
  if (typeof document === "undefined") {
    if (typeof module !== "undefined") {
      module.exports = {ageText: ageText, ageFrom: ageFrom, carryOpen: carryOpen};
    }
    return;
  }

  // Ages ---------------------------------------------------------------
  function tickAges() {
    var now = Date.now();
    var spans = document.querySelectorAll("time[datetime] .age");
    for (var i = 0; i < spans.length; i++) {
      var age = ageFrom(spans[i].parentNode.getAttribute("datetime"), now);
      if (age !== null) spans[i].textContent = age;  // unparseable: keep the server's words
    }
    renderStatus();
  }

  // Status line --------------------------------------------------------
  var builtAt = meta(document, "console-built") || "";
  var period = parseFloat(meta(document, "console-refresh") || "");
  var refreshing = period > 0;
  if (refreshing) period = Math.max(period, MIN_REFRESH_S);
  var failure = null;  // {at: ms, why: text} while the last refresh failed

  function statusText() {
    var parts = [];
    var built = builtAt ? ageFrom(builtAt, Date.now()) : null;
    parts.push(built === null ? "data time unknown" : "updated " + built);
    if (failure) {
      parts.push("refresh failed at " + utcMinute(failure.at) + " (" + failure.why +
                 "), still showing the last data read");
    } else if (refreshing) {
      parts.push("refreshes every " + (period >= 120 ? Math.round(period / 60) + "m" : period + "s"));
    }
    return parts.join(" · ");
  }

  function renderStatus() {
    var el = document.getElementById("live-status");
    if (!el) {
      el = document.createElement("span");
      el.id = "live-status";
      el.className = "live-status";
      var nav = document.querySelector("nav.site-nav, body > nav");
      if (nav) {
        nav.appendChild(el);
      } else {
        document.body.insertBefore(el, document.body.firstChild);
      }
    }
    el.classList.toggle("live-failed", !!failure);
    el.textContent = statusText();
  }

  // Refresh ------------------------------------------------------------
  var timer = null;
  var due = false;

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, period * 1000);
  }

  function describe(list) {
    var out = [];
    for (var i = 0; i < list.length; i++) {
      var d = list[i], s = d.querySelector("summary");
      out.push({id: d.id || "", cls: d.className || "", open: d.open,
                text: s ? s.textContent.replace(/\s+/g, " ").trim() : ""});
    }
    return out;
  }

  function editing() {
    var a = document.activeElement;
    return !!a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
  }

  function refresh() {
    if (document.hidden) { due = true; return; }
    if (editing()) { schedule(); return; }  // never wipe a half-typed query
    due = false;
    fetch(location.href, {headers: {"Accept": "text/html"}, cache: "no-store"})
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (text) {
        var doc = new DOMParser().parseFromString(text, "text/html");
        var stamp = meta(doc, "console-built");
        if (stamp === null) throw new Error("not a console page");
        if (editing()) return;  // the reader started typing while we fetched
        var was = describe(document.querySelectorAll("details"));
        var x = window.scrollX, y = window.scrollY;
        document.body.replaceWith(document.adoptNode(doc.body));
        var now = document.querySelectorAll("details");
        var keep = carryOpen(was, describe(now));
        for (var j = 0; j < now.length; j++) {
          if (keep[j] !== null) now[j].open = keep[j];
        }
        window.scrollTo(x, y);
        // /charts.js re-draws its interactive layer over the new SVGs.
        document.dispatchEvent(new CustomEvent("console:refreshed"));
        builtAt = stamp;
        failure = null;
      })
      .catch(function (err) {
        failure = {at: Date.now(), why: String(err && err.message || err)};
      })
      .then(function () {
        tickAges();
        schedule();
      });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && due) refresh();
  });

  // Pinned sideways scroll bar -----------------------------------------
  // A wide table scrolls inside its own box, and that box's scroll bar sits
  // at the box's bottom edge, which is off screen whenever the reader is
  // part way down a long table (Brian 2026-11-04). While a wide box fills
  // the view and its own bar is below the window, a copy of the bar is
  // pinned to the bottom of the window, at the box's left edge and width,
  // and the two scroll together. Without script the box's own bar remains.
  var WIDE = ".table-scroll, .md-table, .plan-scroll";
  var dock = null, dockInner = null, target = null;

  // Copy one scroll position to the other only when they differ, so the
  // echo of a copy is a no-op and the two can never chase each other.
  function copyScroll(from, to) {
    if (Math.abs(to.scrollLeft - from.scrollLeft) > 1) to.scrollLeft = from.scrollLeft;
  }

  function ensureDock() {
    if (dock && document.body.contains(dock)) return;
    dock = document.createElement("div");
    dock.className = "scroll-dock";
    dock.setAttribute("aria-hidden", "true");
    dockInner = document.createElement("div");
    dock.appendChild(dockInner);
    dock.addEventListener("scroll", function () {
      if (target) copyScroll(dock, target);
    });
    document.body.appendChild(dock);
  }

  function pickTarget() {
    var best = null, bestSeen = 0, vh = window.innerHeight;
    var boxes = document.querySelectorAll(WIDE);
    for (var i = 0; i < boxes.length; i++) {
      var b = boxes[i];
      if (b.scrollWidth <= b.clientWidth + 1) continue;
      var r = b.getBoundingClientRect();
      // Its own bar is on screen: nothing to pin.
      if (r.bottom <= vh) continue;
      var seen = Math.min(r.bottom, vh) - Math.max(r.top, 0);
      if (seen > bestSeen && r.top < vh - 48) { best = b; bestSeen = seen; }
    }
    return best;
  }

  function placeDock() {
    ensureDock();
    target = pickTarget();
    if (!target) { dock.style.display = "none"; return; }
    var r = target.getBoundingClientRect();
    dock.style.display = "block";
    dock.style.left = Math.max(r.left, 0) + "px";
    dock.style.width = target.clientWidth + "px";
    dockInner.style.width = target.scrollWidth + "px";
    copyScroll(target, dock);
  }

  var placing = false;
  function schedulePlace() {
    if (placing) return;
    placing = true;
    window.requestAnimationFrame(function () { placing = false; placeDock(); });
  }

  // Scroll does not bubble; capture sees every box's scroll and the page's.
  document.addEventListener("scroll", function (ev) {
    if (ev.target === dock) return;
    if (target && ev.target === target) copyScroll(target, dock);
    schedulePlace();
  }, true);
  window.addEventListener("resize", schedulePlace);
  window.addEventListener("load", schedulePlace);
  setInterval(schedulePlace, 2000);  // catches a body swapped by a refresh
  schedulePlace();

  // One-line rows -----------------------------------------------------
  // The stylesheet holds every table cell to one line once this runs; a
  // cell cut short says so on hover with its full text, and a click on its
  // row (not on a link or control inside it) opens the row in full.
  document.documentElement.classList.add("js");

  // Each cell's content goes in a `.cell` box the stylesheet can hold to
  // one line; the server's HTML stays as it was, so without script nothing
  // is cut.
  function wrapCells() {
    var tds = document.querySelectorAll(".table-scroll td");
    for (var i = 0; i < tds.length; i++) {
      var td = tds[i];
      if (td.firstElementChild && td.firstElementChild.classList.contains("cell")
          && td.childNodes.length === 1) continue;
      var box = document.createElement("div");
      box.className = "cell";
      while (td.firstChild) box.appendChild(td.firstChild);
      td.appendChild(box);
    }
  }

  function markClipped() {
    wrapCells();
    var cells = document.querySelectorAll(".table-scroll .cell");
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      var cut = c.scrollWidth > c.clientWidth + 1 || c.scrollHeight > c.clientHeight + 1;
      if (cut && !c.closest("tr.row-open")) {
        c.classList.add("clipped");
        if (!c.hasAttribute("title")) c.setAttribute("title", c.textContent.replace(/\s+/g, " ").trim());
      } else if (!c.closest("tr.row-open")) {
        c.classList.remove("clipped");
      }
    }
  }

  document.addEventListener("click", function (ev) {
    if (ev.target.closest("a, button, input, select, textarea, label, summary")) return;
    var row = ev.target.closest(".table-scroll tbody tr, .table-scroll tr");
    if (!row || row.querySelector("th") && !row.querySelector("td")) return;
    if (window.getSelection && String(window.getSelection())) return;  // selecting text
    row.classList.toggle("row-open");
    schedulePlace();
  });
  window.addEventListener("load", markClipped);
  window.addEventListener("resize", markClipped);
  setInterval(markClipped, 2000);  // a refreshed body brings new cells
  markClipped();

  tickAges();
  setInterval(tickAges, TICK_MS);
  if (refreshing) schedule();
})();
