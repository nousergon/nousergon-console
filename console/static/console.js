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

  // Outside a browser (the console's own tests run this file under node)
  // only the pure functions above are exported; nothing below runs.
  if (typeof document === "undefined") {
    if (typeof module !== "undefined") module.exports = {ageText: ageText, ageFrom: ageFrom};
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
        var open = [];
        var was = document.querySelectorAll("details");
        for (var i = 0; i < was.length; i++) open.push(was[i].open);
        var x = window.scrollX, y = window.scrollY;
        document.body.replaceWith(document.adoptNode(doc.body));
        var now = document.querySelectorAll("details");
        if (now.length === open.length) {
          for (var j = 0; j < now.length; j++) now[j].open = open[j];
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

  tickAges();
  setInterval(tickAges, TICK_MS);
  if (refreshing) schedule();
})();
