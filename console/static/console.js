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

   3. A tick (Your actions, and any tick-list table) saves without a page
      load. The box shows ticked at once and the page stays usable, so
      several rows can be ticked in a row; the row moves only when the
      server confirms the write, and a refused or failed write unticks the
      box and says why beside it. Without script the tick is the plain form
      post it always was.

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

  // A tick's reply as {ok, error, ...}. Anything that is not the server's
  // JSON verdict (a login page, a proxy error, a redirect) is a failure:
  // a tick is confirmed only by the console saying so.
  function tickReply(status, contentType, text) {
    var body = null;
    if (/application\/json/.test(contentType || "")) {
      try { body = JSON.parse(text); } catch (e) { body = null; }
    }
    if (body && body.ok === true && status >= 200 && status < 300) return body;
    if (body && typeof body.error === "string" && body.error) {
      return {ok: false, error: body.error};
    }
    return {ok: false, error: "the console did not confirm the save (HTTP " + status +
                              ") — reload and try again"};
  }

  // One tick, start to finish. `ui.optimistic()` runs before anything is
  // sent; then exactly one of `ui.confirm(reply)` / `ui.rollback(message)`.
  // `state.inFlight` counts ticks not yet settled, `state.started` every tick
  // ever begun, and `state.onIdle` runs when the last one settles.
  function tickFlow(ui, send, state) {
    state.inFlight = (state.inFlight || 0) + 1;
    state.started = (state.started || 0) + 1;
    ui.optimistic();
    return Promise.resolve()
      .then(send)
      .then(function (reply) {
        if (reply && reply.ok === true) ui.confirm(reply);
        else ui.rollback((reply && reply.error) || "not saved");
      }, function (err) {
        ui.rollback("not saved: " + String(err && err.message || err || "network error"));
      })
      .then(function () {
        state.inFlight -= 1;
        if (state.inFlight === 0 && state.onIdle) state.onIdle();
      });
  }

  // Whether a page read begun when `state.started` was `seen` predates a tick
  // since: such a read may carry the pre-tick state, so swapping it in would
  // undo a confirmed tick. Idle-time alone could not tell, because a read
  // slower than the quiet window landed after the reader went quiet (codex
  // 52edc3b676051f1b).
  function superseded(seen, state) {
    return (state.started || 0) !== seen;
  }

  // Whether a key pressed on a focused table row (not on a link or control
  // inside it) opens or closes that row: Enter or Space, the keys a button
  // answers to, so a clipped row is not mouse-only (codex e57a4c9e28c3be77).
  function rowToggleKey(key, onRow) {
    return !!onRow && (key === "Enter" || key === " " || key === "Spacebar");
  }

  // Outside a browser (the console's own tests run this file under node)
  // only the pure functions above are exported; nothing below runs.
  if (typeof document === "undefined") {
    if (typeof module !== "undefined") {
      module.exports = {ageText: ageText, ageFrom: ageFrom, carryOpen: carryOpen,
                        tickReply: tickReply, tickFlow: tickFlow, superseded: superseded,
                        rowToggleKey: rowToggleKey, pageActReply: pageActReply};
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

  var ticks = {inFlight: 0};

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
    if (!refreshing) return;
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

  // The reader's last click, key or scroll. A refresh swaps the whole page,
  // so it waits until the reader has been still for QUIET_MS: what they were
  // doing is never undone under them (Brian 2026-11-06).
  var QUIET_MS = 20000;
  var lastActive = 0;
  ["click", "keydown", "scroll", "pointerdown", "wheel", "touchstart"].forEach(function (t) {
    window.addEventListener(t, function () { lastActive = Date.now(); },
                            {capture: true, passive: true});
  });
  function busy() {
    return editing() || ticks.inFlight > 0 || Date.now() - lastActive < QUIET_MS;
  }

  // An expanded table row (`tr.row-open`), named by its first link or, with
  // none, its first cell's text: the same row in the re-read page reopens.
  function rowKey(tr) {
    var a = tr.querySelector("a[href]");
    if (a) return "a:" + a.getAttribute("href");
    var td = tr.querySelector("td");
    return "t:" + (td ? td.textContent.replace(/\s+/g, " ").trim() : "");
  }

  // What the reader is looking at stays exactly where it was across a
  // refresh, even when something above it left the page (Brian 2026-11-11:
  // a ruled decision leaving the list must not move the next one). The
  // anchor is the first element with an id still in view, skipping cards the
  // reader has just ruled, since those are the ones that leave.
  function topAnchor() {
    var els = document.querySelectorAll("body [id]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.closest(".decision-ruled") || el.id === "live-status") continue;
      var r = el.getBoundingClientRect();
      if (r.height > 0 && r.bottom > 0) return {id: el.id, top: r.top};
    }
    return null;
  }
  // A decision ruled on this page stays where it was, as it was, until the
  // reader reloads: the refreshed list no longer carries it, and dropping it
  // would pull everything below it up under the reader's eyes.
  function ruledCards() {
    var out = [];
    var cards = document.querySelectorAll("article.decision.decision-ruled");
    for (var i = 0; i < cards.length; i++) {
      var next = cards[i].nextElementSibling;
      while (next && next.classList.contains("decision-ruled")) next = next.nextElementSibling;
      var prev = cards[i].previousElementSibling;
      while (prev && prev.classList.contains("decision-ruled")) prev = prev.previousElementSibling;
      out.push({node: cards[i], next: next && next.id, prev: prev && prev.id});
    }
    return out;
  }
  function keepRuledCards(ruled) {
    for (var i = 0; i < ruled.length; i++) {
      var r = ruled[i], card = r.node;
      if (card.id) {
        var same = document.getElementById(card.id);
        if (same && same.tagName === "ARTICLE") continue;  // still listed: the server's copy wins
        card.setAttribute("data-ruled", card.id);
        card.removeAttribute("id");  // the Done row carries the id now
      }
      var next = r.next && document.getElementById(r.next);
      var prev = r.prev && document.getElementById(r.prev);
      if (next && next.tagName === "ARTICLE") next.parentNode.insertBefore(card, next);
      else if (prev && prev.tagName === "ARTICLE") prev.parentNode.insertBefore(card, prev.nextSibling);
      else if (i > 0 && ruled[i - 1].node.parentNode) {
        var before = ruled[i - 1].node;
        before.parentNode.insertBefore(card, before.nextSibling);
      }
    }
  }

  function holdAnchor(anchor) {
    if (!anchor) return;
    var el = document.getElementById(anchor.id);
    if (!el) return;
    var shift = el.getBoundingClientRect().top - anchor.top;
    if (shift) window.scrollBy(0, shift);
  }

  function refresh() {
    if (document.hidden) { due = true; return; }
    if (busy()) { retrySoon(); return; }  // never undo what the reader is doing
    due = false;
    var seen = ticks.started || 0;
    fetch(location.href, {headers: {"Accept": "text/html"}, cache: "no-store"})
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (text) {
        var doc = new DOMParser().parseFromString(text, "text/html");
        var stamp = meta(doc, "console-built");
        if (stamp === null) throw new Error("not a console page");
        // The reader acted, or ticked, while we fetched.
        if (busy() || superseded(seen, ticks)) { retry = true; return; }
        var was = describe(document.querySelectorAll("details"));
        var openRows = {};
        var opened = document.querySelectorAll("tr.row-open");
        for (var k = 0; k < opened.length; k++) openRows[rowKey(opened[k])] = true;
        var x = window.scrollX, y = window.scrollY;
        var anchor = topAnchor();
        var ruled = ruledCards();
        document.body.replaceWith(document.adoptNode(doc.body));
        keepRuledCards(ruled);
        var now = document.querySelectorAll("details");
        var keep = carryOpen(was, describe(now));
        for (var j = 0; j < now.length; j++) {
          if (keep[j] !== null) now[j].open = keep[j];
        }
        var rows = document.querySelectorAll(".table-scroll tr");
        for (var r = 0; r < rows.length; r++) {
          if (openRows[rowKey(rows[r])]) rows[r].classList.add("row-open");
        }
        window.scrollTo(x, y);
        holdAnchor(anchor);
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
        if (retry) { retry = false; retrySoon(); } else { schedule(); }
      });
  }

  // A refresh put off because the reader was active tries again shortly
  // rather than a whole period later.
  var retry = false;
  function retrySoon() {
    if (!refreshing) return;
    clearTimeout(timer);
    timer = setTimeout(refresh, 5000);
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
        focusableRow(c.closest("tr"));
        if (!c.hasAttribute("title")) c.setAttribute("title", c.textContent.replace(/\s+/g, " ").trim());
      } else if (!c.closest("tr.row-open")) {
        c.classList.remove("clipped");
      }
    }
  }

  // A row with a cut cell takes keyboard focus and says whether it is open,
  // so the click below has a keyboard twin.
  function focusableRow(tr) {
    if (!tr) return;
    if (!tr.hasAttribute("tabindex")) tr.setAttribute("tabindex", "0");
    tr.setAttribute("aria-expanded", String(tr.classList.contains("row-open")));
  }

  function toggleRow(row) {
    row.classList.toggle("row-open");
    if (row.hasAttribute("aria-expanded")) {
      row.setAttribute("aria-expanded", String(row.classList.contains("row-open")));
    }
    schedulePlace();
  }

  document.addEventListener("click", function (ev) {
    if (ev.target.closest("a, button, input, select, textarea, label, summary")) return;
    var row = ev.target.closest(".table-scroll tbody tr, .table-scroll tr");
    if (!row || row.querySelector("th") && !row.querySelector("td")) return;
    if (window.getSelection && String(window.getSelection())) return;  // selecting text
    toggleRow(row);
  });
  document.addEventListener("keydown", function (ev) {
    var row = ev.target;
    var onRow = row && row.matches && row.matches(".table-scroll tr[tabindex]");
    if (!rowToggleKey(ev.key, onRow)) return;
    ev.preventDefault();  // Space would otherwise scroll the page
    toggleRow(row);
  });
  window.addEventListener("load", markClipped);
  window.addEventListener("resize", markClipped);
  setInterval(markClipped, 2000);  // a refreshed body brings new cells
  markClipped();

  // Copy a command (Your actions): the button copies its box's text and
  // says so; the box itself stays selectable for a browser without the API.
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".action-copy");
    if (!btn) return;
    var code = btn.parentNode.querySelector("pre");
    if (!code || !navigator.clipboard) return;
    navigator.clipboard.writeText(code.textContent).then(function () {
      btn.textContent = "copied";
      setTimeout(function () { btn.textContent = "copy"; }, 1500);
    });
  });

  // Ticks without a page load -------------------------------------------
  var BOX_ON = "\u2611", BOX_OFF = "\u2610";

  function tickUi(form) {
    var btn = form.querySelector("button");
    var input = form.querySelector('input[name="done"]');
    var cell = form.closest("td") || form.parentNode;
    var row = form.closest("tr");
    var was = {text: btn.textContent, pressed: btn.getAttribute("aria-pressed"),
               label: btn.getAttribute("aria-label"), title: btn.getAttribute("title")};
    var marking = input && input.value !== "0";
    function clearError() {
      var old = cell.querySelector(".tick-error");
      if (old) old.parentNode.removeChild(old);
    }
    return {
      optimistic: function () {
        clearError();
        btn.textContent = marking ? BOX_ON : BOX_OFF;
        btn.setAttribute("aria-pressed", marking ? "true" : "false");
        btn.setAttribute("aria-busy", "true");
        btn.disabled = true;  // this row only; every other row stays live
        if (row) row.classList.add("tick-pending");
      },
      confirm: function (reply) {
        if (row) row.classList.remove("tick-pending");
        btn.removeAttribute("aria-busy");
        if (typeof reply.row === "string" && row) {
          moveActionRow(row, reply.row, !reply.actionable);
          return;
        }
        // A tick-list row: it stays where it is, now saved the other way.
        btn.disabled = false;
        if (input) input.value = marking ? "0" : "1";
        var word = (was.label || "").replace(/^(Mark|Unmark) /, "");
        btn.setAttribute("aria-label", (marking ? "Unmark " : "Mark ") + word);
        btn.setAttribute("title", marking ? "Saved — click to unmark" : "Saved — click to mark");
      },
      rollback: function (message) {
        if (row) row.classList.remove("tick-pending");
        btn.removeAttribute("aria-busy");
        btn.disabled = false;
        btn.textContent = was.text;
        if (was.pressed !== null) btn.setAttribute("aria-pressed", was.pressed);
        clearError();
        var err = document.createElement("span");
        err.className = "tick-error";
        err.setAttribute("role", "alert");
        err.textContent = message;
        cell.appendChild(err);
      }
    };
  }

  // The confirmed row, re-rendered by the server, goes to the table it now
  // belongs in: Done when it is no longer waiting on the reader, To do when
  // it is again. With no such table on the page the row is replaced where it
  // stands; the cadence refresh, once the reader is still, lays the page out
  // afresh with the missing section (see the note above the submit handler).
  function moveActionRow(row, html, done) {
    var tpl = document.createElement("template");
    tpl.innerHTML = "<table><tbody>" + html + "</tbody></table>";
    var fresh = tpl.content.querySelector("tr");
    if (!fresh) return;
    var tables = document.querySelectorAll("table.actions");
    var target = null;
    for (var i = 0; i < tables.length; i++) {
      var inDone = !!tables[i].closest("details.actions-done");
      if (inDone === done) { target = tables[i].tBodies[0]; break; }
    }
    if (!target || target.contains(row)) {
      row.parentNode.replaceChild(fresh, row);
      return;
    }
    // Fade the row out where it stands, then add it to the other table and
    // recount. Nothing else on the page is touched (Brian 2026-11-06).
    row.classList.add("tick-leaving");
    setTimeout(function () {
      if (row.parentNode) row.parentNode.removeChild(row);
      fresh.classList.add("tick-arrived");
      target.insertBefore(fresh, target.firstChild);
      recountActions();
      setTimeout(function () { fresh.classList.remove("tick-arrived"); }, 1600);
    }, 350);
  }

  // The "N to do · M done" line and the Done heading, counted from the rows
  // now on the page, so they agree with the move without a re-read.
  function recountActions() {
    var tables = document.querySelectorAll("table.actions");
    var todo = 0, done = 0;
    for (var i = 0; i < tables.length; i++) {
      var n = tables[i].tBodies[0] ? tables[i].tBodies[0].rows.length : 0;
      if (tables[i].closest("details.actions-done")) done += n;
      else if (!tables[i].closest("details.actions-waiting")) todo += n;
    }
    var h = document.querySelector("details.actions-done > summary > h2");
    if (h) h.textContent = "Done \u00b7 " + done;
    var ps = document.querySelectorAll("body > p");
    for (var j = 0; j < ps.length; j++) {
      var t = ps[j].firstChild;
      if (t && t.nodeType === 3 && /^\d+ to do \u00b7 \d+ done \u00b7 /.test(t.nodeValue)) {
        t.nodeValue = t.nodeValue.replace(/^\d+ to do \u00b7 \d+ done/, todo + " to do \u00b7 " + done + " done");
      }
    }
  }

  function sendTick(form) {
    var body = new URLSearchParams(new FormData(form)).toString();
    return fetch(form.getAttribute("action"), {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"},
      body: body
    }).then(function (resp) {
      return resp.text().then(function (text) {
        return tickReply(resp.status, resp.headers.get("Content-Type"), text);
      });
    });
  }

  // A settled tick does NOT re-read the page: the row already moved and the
  // counts were redone in place. The cadence refresh, once the reader is
  // still, brings the rest (Brian 2026-11-06: a re-read right after a tick
  // undid what he had done in the seconds since).

  // A ruling (Brian 2026-11-11): the pick saves in the background like a
  // tick. The card stays where it is and only its own buttons lock; the
  // reader keeps scrolling to the next decision. Confirmed, the card says
  // which option was saved; refused, it says why and the buttons come back.
  function optionUi(form) {
    var card = form.closest("article.decision");
    var btn = form.querySelector("button");
    var buttons = card ? card.querySelectorAll(".decision-options button") : [btn];
    var choice = form.querySelector('input[name="choice"]');
    var letter = choice ? choice.value : "";
    function clearError() {
      var old = card && card.querySelector(".tick-error");
      if (old) old.parentNode.removeChild(old);
    }
    function lock(on) {
      for (var i = 0; i < buttons.length; i++) buttons[i].disabled = on;
    }
    return {
      optimistic: function () {
        clearError();
        lock(true);
        btn.setAttribute("aria-busy", "true");
        btn.classList.add("decision-picked");
        if (card) card.classList.add("decision-pending");
      },
      confirm: function () {
        // Nothing here changes the card's size (Brian 2026-11-11: the page
        // must not shift when a pick lands): the buttons stay, locked, and
        // the saved note floats over the card's corner.
        btn.removeAttribute("aria-busy");
        if (!card) return;
        card.classList.remove("decision-pending");
        card.classList.add("decision-ruled");
        var done = document.createElement("span");
        done.className = "decision-saved";
        done.setAttribute("role", "status");
        done.textContent = "Saved: " + letter;
        card.appendChild(done);
      },
      rollback: function (message) {
        btn.removeAttribute("aria-busy");
        btn.classList.remove("decision-picked");
        if (card) card.classList.remove("decision-pending");
        lock(false);
        clearError();
        var err = document.createElement("span");
        err.className = "tick-error decision-error";
        err.setAttribute("role", "alert");
        err.textContent = message;
        (card || form).appendChild(err);
      }
    };
  }

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains("action-option")) return;
    if (!window.fetch || !window.URLSearchParams || !window.FormData) return;  // plain post
    ev.preventDefault();
    var btn = form.querySelector("button");
    if (!btn || btn.disabled) return;
    tickFlow(optionUi(form), function () { return sendTick(form); }, ticks);
  });

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains("action-tick")) return;
    if (!window.fetch || !window.URLSearchParams || !window.FormData) return;  // plain post
    ev.preventDefault();
    var btn = form.querySelector("button");
    if (!btn || btn.disabled) return;
    tickFlow(tickUi(form), function () { return sendTick(form); }, ticks);
  });

  // A page's declared decision (a `decisions` section, Brian 2026-11-13).
  // The button posts JSON to the section's declared writer path, never to
  // the console; the writer answers JSON, and only a 2xx JSON answer with
  // no `error` is a save. Same flow and refresh guard as a ruling above.
  function pageActReply(status, contentType, text) {
    var body = null;
    if (/application\/json/.test(contentType || "")) {
      try { body = JSON.parse(text); } catch (e) { body = null; }
    }
    if (body && typeof body.error === "string" && body.error) {
      return {ok: false, error: body.error + " (HTTP " + status + ")"};
    }
    if (body && status >= 200 && status < 300 && body.ok !== false) {
      body.ok = true;
      return body;
    }
    return {ok: false, error: "the decision was not confirmed (HTTP " + status +
                              ") — reload and try again"};
  }

  function pageActBody(form, btn) {
    var body = {};
    var inputs = form.querySelectorAll('input[type="hidden"]');
    for (var i = 0; i < inputs.length; i++) body[inputs[i].name] = inputs[i].value;
    body[form.getAttribute("data-choice-field") || "choice"] = btn.getAttribute("data-choice");
    var note = form.querySelector('textarea[name="note"]');
    if (note && note.value.replace(/\s+/g, "")) body.note = note.value.trim();
    if (btn.getAttribute("data-change")) {
      var selects = form.querySelectorAll("details.decision-change select");
      for (var j = 0; j < selects.length; j++) body[selects[j].name] = selects[j].value;
    }
    return body;
  }

  function pageActUi(form, btn) {
    var card = form.closest("article.decision");
    var buttons = form.querySelectorAll("button[data-choice]");
    var label = (btn.textContent || "").replace(/^Save:\s*/, "").trim();
    function clearError() {
      var old = card && card.querySelector(".tick-error");
      if (old) old.parentNode.removeChild(old);
    }
    function lock(on) {
      for (var i = 0; i < buttons.length; i++) buttons[i].disabled = on;
      var fields = form.querySelectorAll("textarea, select");
      for (var k = 0; k < fields.length; k++) fields[k].disabled = on;
    }
    return {
      optimistic: function () {
        clearError();
        lock(true);
        btn.setAttribute("aria-busy", "true");
        btn.classList.add("decision-picked");
        if (card) card.classList.add("decision-pending");
      },
      confirm: function (reply) {
        btn.removeAttribute("aria-busy");
        if (!card) return;
        card.classList.remove("decision-pending");
        card.classList.add("decision-ruled");
        var done = document.createElement("span");
        done.className = "decision-saved";
        done.setAttribute("role", "status");
        done.textContent = "Saved: " + label +
          (reply && reply.decided_by ? " by " + reply.decided_by : "");
        card.appendChild(done);
      },
      rollback: function (message) {
        btn.removeAttribute("aria-busy");
        btn.classList.remove("decision-picked");
        if (card) card.classList.remove("decision-pending");
        lock(false);
        clearError();
        var err = document.createElement("span");
        err.className = "tick-error decision-error";
        err.setAttribute("role", "alert");
        err.textContent = message;
        (card || form).appendChild(err);
      }
    };
  }

  function sendPageAct(form, btn) {
    var post = form.getAttribute("data-post") || "";
    // Same origin only, and never the console's own path (the server
    // refuses such a declaration; this is the browser-side half).
    if (!/^\/[A-Za-z0-9]/.test(post) || post.indexOf("//") !== -1) {
      return Promise.resolve({ok: false, error: "this page declares no valid place to save to"});
    }
    return fetch(post, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Accept": "application/json", "Content-Type": "application/json"},
      body: JSON.stringify(pageActBody(form, btn))
    }).then(function (resp) {
      return resp.text().then(function (text) {
        return pageActReply(resp.status, resp.headers.get("Content-Type"), text);
      });
    });
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest && ev.target.closest("form.page-act button[data-choice]");
    if (!btn || btn.disabled) return;
    if (!window.fetch || !window.JSON) return;
    ev.preventDefault();
    var form = btn.closest("form.page-act");
    tickFlow(pageActUi(form, btn), function () { return sendPageAct(form, btn); }, ticks);
  });

  tickAges();
  setInterval(tickAges, TICK_MS);
  if (refreshing) schedule();
})();
