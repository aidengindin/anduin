// Chart glue for anduin. Finds every `.chart[data-chart]` and renders it with
// uPlot. Three kinds:
//   metric-line   — fetches data-url JSON {points:[{t,avg,min,max}], unit}
//   stream-line   — reads data-series JSON [{t, v}]
//   summary-bars  — reads data-series JSON [{t, steps, energy}]
// All timestamps are epoch seconds (uPlot's native x scale).

(function () {
  "use strict";

  function sizeFor(el) {
    const h = parseInt(getComputedStyle(el).minHeight, 10) || 260;
    return { width: el.clientWidth || 600, height: h };
  }

  // Dark-theme axis styling. uPlot paints axes on <canvas>, so colours can't be
  // set from CSS — they go through options. Light labels/ticks, faint grid.
  const AXIS_LABEL = "#c0caf5";
  const AXIS_TICK = "rgba(192,202,245,0.35)";
  const AXIS_GRID = "rgba(192,202,245,0.10)";
  function axis(extra) {
    return Object.assign(
      {
        stroke: AXIS_LABEL,
        grid: { stroke: AXIS_GRID, width: 1 },
        ticks: { stroke: AXIS_TICK, width: 1 },
      },
      extra || {}
    );
  }

  // Re-fit every uPlot instance to its container on resize.
  const instances = [];
  function trackResize(u, el) {
    instances.push({ u: u, el: el });
  }
  let resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      instances.forEach(function (rec) {
        rec.u.setSize(sizeFor(rec.el));
      });
    }, 120);
  });

  function emptyMsg(el, text) {
    el.innerHTML = '<p class="chart-empty">' + text + "</p>";
  }

  function lineChart(el, xs, series, unit, bands) {
    if (!xs.length) {
      emptyMsg(el, "No data in this range.");
      return;
    }
    const opts = Object.assign(
      {
        title: "",
        cursor: { y: false },
        legend: { show: false },
        scales: { x: { time: true } },
        axes: [axis(), axis({ size: 60, label: unit || "" })],
        bands: bands || [],
        series: series,
      },
      sizeFor(el)
    );
    const data = [xs].concat(series.slice(1).map(function (s) { return s._vals; }));
    const u = new uPlot(opts, data, el);
    trackResize(u, el);
  }

  function renderMetric(el) {
    fetch(el.dataset.url)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        const pts = d.points || [];
        const xs = pts.map(function (p) { return p.t; });
        const avg = pts.map(function (p) { return p.avg; });
        const min = pts.map(function (p) { return p.min; });
        const max = pts.map(function (p) { return p.max; });
        const color = el.dataset.color || "#34d3c2";
        const bandByT = new Map((d.band || []).map(function (p) { return [p.t, p]; }));
        const lo = xs.map(function (t) {
          const b = bandByT.get(t);
          return b ? b.lo : null;
        });
        const hi = xs.map(function (t) {
          const b = bandByT.get(t);
          return b ? b.hi : null;
        });
        const series = [
          {},
        ];
        const bands = [];
        if ((d.band || []).length) {
          const loIdx = series.length;
          series.push({ label: "normal low", stroke: "transparent", _vals: lo });
          const hiIdx = series.length;
          series.push({ label: "normal high", stroke: "transparent", _vals: hi });
          bands.push({ series: [hiIdx, loIdx], fill: color + "1f" });
        }
        series.push(
          { label: "min", stroke: color + "33", _vals: min },
          { label: "max", stroke: color + "33", _vals: max },
          { label: d.label || "avg", stroke: color, width: 2.4, _vals: avg },
        );
        lineChart(el, xs, series, d.unit, bands);
      })
      .catch(function () { emptyMsg(el, "Failed to load."); });
  }

  function renderStream(el) {
    const pts = JSON.parse(el.dataset.series || "[]");
    const xs = pts.map(function (p) { return p.t; });
    const vs = pts.map(function (p) { return p.v; });
    lineChart(el, xs, [{}, { label: "value", stroke: "#7aa2f7", width: 1.5, _vals: vs }], "");
  }

  function renderSummaryBars(el) {
    const pts = JSON.parse(el.dataset.series || "[]");
    if (!pts.length) { emptyMsg(el, "No data in this range."); return; }
    const xs = pts.map(function (p) { return p.t; });
    const steps = pts.map(function (p) { return p.steps; });
    const energy = pts.map(function (p) { return p.energy; });
    const barsPath = uPlot.paths.bars ? uPlot.paths.bars({ size: [0.6, 100] }) : null;
    const opts = Object.assign(
      {
        cursor: { y: false },
        scales: { x: { time: true }, kcal: {} },
        axes: [
          axis(),
          axis({ scale: "steps", size: 60, label: "steps" }),
          axis({ scale: "kcal", side: 1, size: 60, label: "kcal" }),
        ],
        series: [
          {},
          { label: "steps", scale: "steps", stroke: "#7aa2f7", fill: "#7aa2f766", paths: barsPath },
          { label: "energy", scale: "kcal", stroke: "#e0af68", width: 2 },
        ],
      },
      sizeFor(el)
    );
    const u = new uPlot(opts, [xs, steps, energy], el);
    trackResize(u, el);
  }

  function init() {
    if (typeof uPlot === "undefined") { return; }
    document.querySelectorAll(".chart[data-chart]").forEach(function (el) {
      switch (el.dataset.chart) {
        case "metric-line": renderMetric(el); break;
        case "stream-line": renderStream(el); break;
        case "summary-bars": renderSummaryBars(el); break;
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
