/* charts.js — interactive charts over the server's SVG, as progressive
   enhancement only (alpha-engine-config-I12783, plan §3: "hover, series
   toggle ... from a small pinned library ... the server-side SVG remains the
   no-JS fallback").

   Every chart is complete without this file: the server draws it as an SVG
   with a `<title>` per bar and the span, latest, peak and reference printed
   beside it. With script on, a chart whose SVG carries `data-chart` (the same
   points at full precision, `_chart_data` in console/render/html.py) and sits
   in a page chart or the exception trend gets a uPlot canvas in its place:

   1. Hover (or touch) shows the point's label and every shown series' exact
      value in a tooltip, and the legend reads the same values live.
   2. Each series in the legend is a toggle (click, Enter or Space). What is
      hidden is written to the URL fragment (`#off=<chart>~<series>,...`), so
      a pasted link reproduces the view; the server never sees it.
   3. Dragging across the plot zooms the x range; a double-click resets it.

   Colours are the design tokens, read from the stylesheet at draw time, so a
   chart follows the scheme like everything else; a scheme change redraws.
   The library is uPlot, vendored at /vendor/uplot/ (same origin, pinned by
   digest, licence beside it) and loaded only on a page that has a chart.
   The SVG stays in the page, hidden, and its summary text stays visible. */
(function () {
  "use strict";

  var SELECTOR = "figure.page-chart svg[data-chart], .exception-trend svg[data-chart]";
  var LIB = "/vendor/uplot/uPlot.iife.min.js";
  var LIB_CSS = "/vendor/uplot/uPlot.min.css";
  // Series colour tokens, in order; a reference line takes `--chart-ref`.
  var BAR_TOKENS = ["chart-1", "chart-2", "chart-3"];

  // Pure helpers (exported under node for the console's tests) ---------
  function formatValue(v, unit) {
    if (v === null || v === undefined) return "not measured";
    return String(v) + (unit ? " " + unit : "");
  }

  function hiddenFrom(hash) {
    var out = {};
    var m = /(?:^#|&)off=([^&]*)/.exec(hash || "");
    if (!m || !m[1]) return out;
    m[1].split(",").forEach(function (pair) {
      var bits = pair.split("~");
      if (bits.length !== 2) return;
      var key = decodeURIComponent(bits[0]), label = decodeURIComponent(bits[1]);
      (out[key] = out[key] || {})[label] = true;
    });
    return out;
  }

  // `~` separates chart from series, and encodeURIComponent leaves it bare.
  function enc(text) {
    return encodeURIComponent(text).replace(/~/g, "%7E");
  }

  function hashFor(hidden, hash) {
    var pairs = [];
    Object.keys(hidden).sort().forEach(function (key) {
      Object.keys(hidden[key]).sort().forEach(function (label) {
        pairs.push(enc(key) + "~" + enc(label));
      });
    });
    var rest = (hash || "").replace(/^#/, "").split("&").filter(function (p) {
      return p && p.indexOf("off=") !== 0;
    });
    if (pairs.length) rest.push("off=" + pairs.join(","));
    return rest.length ? "#" + rest.join("&") : "";
  }

  function withAlpha(colour, alpha) {
    var m = /^#([0-9a-f]{6})$/i.exec(colour);
    if (!m) return colour;
    var n = parseInt(m[1], 16);
    return "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")";
  }

  function yRange(series) {
    var lo = 0, hi = 0;
    series.forEach(function (s) {
      s.values.forEach(function (v) {
        if (v === null) return;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      });
    });
    if (lo === hi) hi = lo + 1;
    var pad = (hi - lo) * 0.05;
    return [lo < 0 ? lo - pad : 0, hi > 0 ? hi + pad : 0];
  }

  if (typeof document === "undefined") {
    if (typeof module !== "undefined") {
      module.exports = {formatValue: formatValue, hiddenFrom: hiddenFrom, hashFor: hashFor,
                        withAlpha: withAlpha, yRange: yRange};
    }
    return;
  }

  // Loading -------------------------------------------------------------
  var loading = null;

  function library() {
    if (window.uPlot) return Promise.resolve(window.uPlot);
    if (loading) return loading;
    loading = new Promise(function (resolve, reject) {
      var css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = LIB_CSS;
      // Before /styles.css, so the console's token overrides win.
      var first = document.querySelector('link[rel="stylesheet"]');
      document.head.insertBefore(css, first);
      var s = document.createElement("script");
      s.src = LIB;
      s.onload = function () { window.uPlot ? resolve(window.uPlot) : reject(new Error("uPlot missing")); };
      s.onerror = function () { reject(new Error("could not load " + LIB)); };
      document.head.appendChild(s);
    });
    return loading;
  }

  // Charts --------------------------------------------------------------
  var live = [];

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue("--" + name).trim();
  }

  function marksOf(svg, n) {
    // Event marks the server drew (`line.ts-mark`, one per annotated point)
    // are carried across by their slot: x = pad + (i + 0.5) * slot.
    var vb = (svg.getAttribute("viewBox") || "").split(/\s+/).map(Number);
    var width = vb[2] || 0, pad = 2, slot = (width - 2 * pad) / n;
    var out = [];
    var lines = svg.querySelectorAll("line.ts-mark");
    for (var i = 0; i < lines.length; i++) {
      var x = parseFloat(lines[i].getAttribute("x1"));
      if (!(slot > 0) || isNaN(x)) continue;
      out.push(Math.round((x - pad) / slot - 0.5));
    }
    return out;
  }

  function tooltip(u, data, tip) {
    var idx = u.cursor.idx;
    if (idx === null || idx === undefined || u.cursor.left < 0) {
      tip.hidden = true;
      return;
    }
    var lines = [data.labels[idx]];
    for (var i = 1; i < u.series.length; i++) {
      if (!u.series[i].show) continue;
      if (u.data[i][idx] === null && data.series[i - 1].sparse) continue;
      lines.push(u.series[i].label + ": " + formatValue(u.data[i][idx], data.unit));
    }
    tip.textContent = lines.join("\n");
    tip.hidden = false;
    var room = u.over.clientWidth;
    var left = u.cursor.left + 12;
    if (left + tip.offsetWidth > room) left = Math.max(0, u.cursor.left - tip.offsetWidth - 12);
    tip.style.left = left + "px";
    tip.style.top = Math.max(0, u.cursor.top - tip.offsetHeight - 8) + "px";
  }

  function keyboardLegend(u) {
    var rows = u.root.querySelectorAll(".u-legend .u-series");
    for (var i = 1; i < rows.length; i++) {
      (function (row, si) {
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        row.setAttribute("aria-pressed", String(u.series[si].show));
        row.setAttribute("aria-label", "show " + u.series[si].label);
        row.addEventListener("keydown", function (ev) {
          if (ev.key !== "Enter" && ev.key !== " ") return;
          ev.preventDefault();
          u.setSeries(si, {show: !u.series[si].show});
        });
      })(rows[i], i);
    }
  }

  function enhance(uPlot, svg) {
    var data;
    try {
      data = JSON.parse(svg.getAttribute("data-chart"));
    } catch (e) {
      return;  // a malformed payload leaves the server's SVG in place
    }
    if (!data || !data.labels || !data.labels.length || !data.series) return;
    var n = data.labels.length;
    var hidden = hiddenFrom(location.hash)[data.key] || {};
    var host = document.createElement("div");
    host.className = "chart-live";
    svg.parentNode.insertBefore(host, svg);
    svg.classList.add("ts-enhanced");

    var muted = token("muted"), border = token("border"), text = token("text");
    var bars = uPlot.paths.bars({size: [0.8, Infinity], align: 0});
    var xs = [], seriesOpts = [{
      label: "at",
      value: function (u, v) { return v === null || v === undefined ? "—" : data.labels[v]; },
    }];
    for (var i = 0; i < n; i++) xs.push(i);
    var plotted = [xs], barNo = 0;
    data.series.forEach(function (s) {
      var line = s.kind === "line";
      var colour = line ? token("chart-ref") : token(BAR_TOKENS[barNo++ % BAR_TOKENS.length]);
      plotted.push(s.values);
      seriesOpts.push({
        label: s.label,
        show: !hidden[s.label],
        stroke: colour,
        width: line ? 1.5 : 1,
        dash: line ? [4, 3] : undefined,
        fill: line ? undefined : withAlpha(colour, 0.55),
        fillTo: 0,
        paths: line ? undefined : bars,
        points: {show: false},
        // At rest (no point under the cursor) the legend names no value; a
        // gap reads "not measured", except in a sparse series, where a null
        // only means the point belongs to another series.
        value: function (u, v) {
          if (u.cursor.idx === null || u.cursor.idx === undefined) return "—";
          return v === null && s.sparse ? "—" : formatValue(v, data.unit);
        },
      });
    });
    var marks = marksOf(svg, n);
    var tall = !!svg.closest("figure.page-chart");
    var height = tall ? 220 : 150;
    var tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.hidden = true;
    var axis = {stroke: muted, grid: {stroke: border, width: 1}, ticks: {stroke: border, width: 1},
                font: "12px system-ui, sans-serif"};
    var opts = {
      width: Math.max(host.clientWidth, 200),
      height: height,
      series: seriesOpts,
      scales: {
        x: {time: false, range: function (u, lo, hi) {
          return [Math.min(lo, 0) - 0.5, Math.max(hi, n - 1) + 0.5];
        }},
        y: {range: function () { return yRange(data.series); }},
      },
      axes: [
        Object.assign({}, axis, {
          space: 70,
          incrs: [1, 2, 5, 10, 20, 50, 100, 200, 500],
          values: function (u, splits) {
            return splits.map(function (v) {
              return Number.isInteger(v) && v >= 0 && v < n ? data.labels[v] : "";
            });
          },
        }),
        Object.assign({}, axis, {size: 50}),
      ],
      cursor: {drag: {x: true, y: false}, points: {show: false}},
      legend: {live: true},
      hooks: {
        init: [function (u) {
          u.over.appendChild(tip);
          u.root.style.color = text;
          keyboardLegend(u);
        }],
        setCursor: [function (u) { tooltip(u, data, tip); }],
        setSeries: [function (u, si) {
          if (si === null || si === undefined || si < 1) return;
          var all = hiddenFrom(location.hash);
          var mine = all[data.key] || {};
          if (u.series[si].show) delete mine[u.series[si].label];
          else mine[u.series[si].label] = true;
          if (Object.keys(mine).length) all[data.key] = mine;
          else delete all[data.key];
          history.replaceState(null, "", location.pathname + location.search + hashFor(all, location.hash));
          var row = u.root.querySelectorAll(".u-legend .u-series")[si];
          if (row) row.setAttribute("aria-pressed", String(u.series[si].show));
          tooltip(u, data, tip);
        }],
        draw: [function (u) {
          if (!marks.length) return;
          var ctx = u.ctx;
          ctx.save();
          ctx.strokeStyle = muted;
          ctx.lineWidth = 1;
          ctx.setLineDash([2, 3]);
          marks.forEach(function (i) {
            var x = Math.round(u.valToPos(i, "x", true)) + 0.5;
            ctx.beginPath();
            ctx.moveTo(x, u.bbox.top);
            ctx.lineTo(x, u.bbox.top + u.bbox.height);
            ctx.stroke();
          });
          ctx.restore();
        }],
      },
    };
    var u = new uPlot(opts, plotted, host);
    live.push({u: u, host: host, svg: svg, height: height});
  }

  function enhanceAll() {
    var svgs = document.querySelectorAll(SELECTOR);
    var todo = [];
    for (var i = 0; i < svgs.length; i++) {
      if (!svgs[i].classList.contains("ts-enhanced")) todo.push(svgs[i]);
    }
    if (!todo.length) return;
    library().then(function (uPlot) {
      todo.forEach(function (svg) {
        try {
          enhance(uPlot, svg);
        } catch (e) {
          svg.classList.remove("ts-enhanced");  // the SVG stays the chart
        }
      });
    }, function () { /* no library: the server's SVG is the chart */ });
  }

  function teardown() {
    live.forEach(function (c) {
      c.u.destroy();
      if (c.host.parentNode) c.host.parentNode.removeChild(c.host);
      c.svg.classList.remove("ts-enhanced");
    });
    live = [];
  }

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      live.forEach(function (c) {
        c.u.setSize({width: Math.max(c.host.clientWidth, 200), height: c.height});
      });
    }, 100);
  });
  // A scheme change re-reads the tokens; a refreshed body has new SVGs.
  if (window.matchMedia) {
    var scheme = window.matchMedia("(prefers-color-scheme: dark)");
    if (scheme.addEventListener) scheme.addEventListener("change", function () { teardown(); enhanceAll(); });
  }
  document.addEventListener("console:refreshed", function () { live = []; enhanceAll(); });

  enhanceAll();
})();
