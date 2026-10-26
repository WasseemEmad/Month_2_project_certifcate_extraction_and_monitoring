/* ==========================================================================
   Certificate Extraction Monitor - Front-End
   ========================================================================== */
"use strict";

const COLORS = {
  accent: "hsl(172 72% 52%)",
  ref: "hsl(262 85% 74%)",
  threshold: "hsl(36 95% 60%)",
  critical: "hsl(350 85% 62%)",
  ok: "hsl(150 65% 52%)",
  faint: "hsla(220, 30%, 75%, 0.28)",
};

let DATA = null;
let shareTestIdx = null;
let sevFilter = "all";
let currentView = "overview";

// ---------------------------------------------------------------- helpers
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = {
  int: (v) => (v == null ? "—" : Number(v).toLocaleString("en-US")),
  ms: (v) => (v == null ? "—" : `${Math.round(v).toLocaleString("en-US")} ms`),
  pct: (v, d = 1) => (v == null ? "—" : `${Number(v).toFixed(d)}%`),
  usd: (v) => (v == null ? "—" : `$${Number(v).toFixed(4)}`),
  num: (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d)),
  pp: (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${Number(v).toFixed(1)} pp`),
};
const badge = (cls, text, dot = true) => `<span class="badge ${cls}">${dot ? '<span class="dot"></span>' : ""}${esc(text)}</span>`;
const sevBadge = (sev) => badge(sev.toLowerCase(), sev.toUpperCase());
const titleCase = (s) => String(s).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

function niceTicks(min, max, count = 5) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

// ---------------------------------------------------------------- SVG line chart
function lineChart(el, opts) {
  if (!el) return;
  el._opts = opts;
  const W = Math.max(320, el.clientWidth || 600);
  const H = opts.height || 240;
  const m = { l: 56, r: 18, t: 18, b: opts.categorical ? 40 : 30 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const cat = typeof opts.x[0] === "string";
  const n = opts.x.length;

  const vals = [];
  opts.series.forEach((s) => s.values.forEach((v) => v != null && vals.push(v)));
  (opts.thresholds || []).forEach((t) => vals.push(t.value));
  if (opts.refLine) vals.push(opts.refLine.value);
  let yMin = opts.yMin ?? Math.min(...vals);
  let yMax = opts.yMax ?? Math.max(...vals);
  const pad = (yMax - yMin || Math.abs(yMax) || 1) * 0.12;
  if (opts.yMin == null) yMin -= pad;
  if (opts.yMax == null) yMax += pad;
  const ticks = niceTicks(yMin, yMax, 5);
  yMin = opts.yMin ?? ticks[0];
  yMax = opts.yMax ?? ticks[ticks.length - 1];

  const xs = cat
    ? (i) => m.l + (n === 1 ? iw / 2 : (iw * (i + 0.5)) / n)
    : (() => { const x0 = opts.x[0], x1 = opts.x[n - 1]; return (i) => m.l + ((opts.x[i] - x0) / (x1 - x0 || 1)) * iw; })();
  const xv = (v) => m.l + ((v - opts.x[0]) / (opts.x[n - 1] - opts.x[0] || 1)) * iw;
  const ys = (v) => m.t + ih - ((v - yMin) / (yMax - yMin || 1)) * ih;
  const yFmt = opts.yFmt || ((v) => v);

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(opts.aria || "chart")}">`;

  if (opts.refBand && !cat) {
    const x1 = xv(opts.refBand.from - 0.5), x2 = xv(opts.refBand.to + 0.5);
    svg += `<rect class="ref-band" x="${Math.max(m.l, x1)}" y="${m.t}" width="${x2 - Math.max(m.l, x1)}" height="${ih}" rx="6"/>`;
    svg += `<text class="ref-band-label" x="${Math.max(m.l, x1) + 8}" y="${m.t + 14}">${esc(opts.refBand.label)}</text>`;
  }
  ticks.forEach((t) => {
    if (t < yMin - 1e-9 || t > yMax + 1e-9) return;
    svg += `<line class="grid-line" x1="${m.l}" x2="${W - m.r}" y1="${ys(t)}" y2="${ys(t)}"/>`;
    svg += `<text class="axis-label" x="${m.l - 8}" y="${ys(t) + 4}" text-anchor="end">${esc(yFmt(t))}</text>`;
  });
  if (cat) {
    opts.x.forEach((lbl, i) => {
      const [a, b] = String(lbl).split("\n");
      svg += `<text class="axis-label" x="${xs(i)}" y="${H - m.b + 16}" text-anchor="middle" ${i === 0 && opts.firstIsRef ? `style="fill:${COLORS.ref};font-weight:600"` : ""}>${esc(a)}</text>`;
      if (b) svg += `<text class="axis-label" x="${xs(i)}" y="${H - m.b + 30}" text-anchor="middle">${esc(b)}</text>`;
    });
  } else {
    niceTicks(opts.x[0], opts.x[n - 1], 8).forEach((t) => {
      if (t < opts.x[0] || t > opts.x[n - 1]) return;
      svg += `<text class="axis-label" x="${xv(t)}" y="${H - m.b + 18}" text-anchor="middle">${esc(opts.xFmt ? opts.xFmt(t) : t)}</text>`;
    });
  }
  if (opts.refLine) {
    const y = ys(opts.refLine.value);
    svg += `<line class="ref-line" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/>`;
    svg += `<text class="ref-label" x="${m.l + 6}" y="${y - 6}">${esc(opts.refLine.label)}</text>`;
  }
  (opts.thresholds || []).forEach((t) => {
    const y = ys(t.value);
    const cls = t.secondary ? "threshold secondary" : "threshold";
    svg += `<line class="${cls}" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/>`;
    svg += `<text class="threshold-label${t.secondary ? " secondary" : ""}" x="${W - m.r - 4}" y="${y - 6}" text-anchor="end">${esc(t.label)}</text>`;
  });
  opts.series.forEach((s, si) => {
    let d = "", pen = false;
    s.values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${xs(i).toFixed(1)},${ys(v).toFixed(1)}`;
      pen = true;
    });
    svg += `<path class="series-line ${s.animate === false ? "" : "draw"}" data-si="${si}" d="${d}" stroke="${s.color}" style="stroke-width:${s.width || 2.4}"/>`;
    if (s.points) {
      s.values.forEach((v, i) => {
        if (v == null) return;
        const c = s.pointColor ? s.pointColor(i, v) : s.color;
        svg += `<circle class="pt" cx="${xs(i)}" cy="${ys(v)}" r="5" fill="${c}"/>`;
        if (s.labels) svg += `<text class="pt-label" x="${xs(i)}" y="${ys(v) - 11}" text-anchor="middle">${esc((s.labelFmt || yFmt)(v, i))}</text>`;
      });
    }
  });
  svg += `<line class="hover-line" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" visibility="hidden"/>`;
  svg += `<rect x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent" class="overlay"/>`;
  svg += `</svg>`;

  el.innerHTML = svg + `<div class="tooltip"></div>` + (opts.legend ? `<div class="legend">${opts.legend}</div>` : "");

  el.querySelectorAll("path.series-line.draw").forEach((p) => {
    const len = Math.ceil(p.getTotalLength());
    p.style.setProperty("--len", len);
  });

  if (opts.tooltip) {
    const tip = el.querySelector(".tooltip");
    const hl = el.querySelector(".hover-line");
    const overlay = el.querySelector(".overlay");
    const svgEl = el.querySelector("svg");
    overlay.addEventListener("mousemove", (ev) => {
      const rect = svgEl.getBoundingClientRect();
      const px = ((ev.clientX - rect.left) / rect.width) * W;
      let best = 0, bd = Infinity;
      for (let i = 0; i < n; i++) { const dd = Math.abs(xs(i) - px); if (dd < bd) { bd = dd; best = i; } }
      const html = opts.tooltip(best);
      if (!html) return;
      hl.setAttribute("x1", xs(best)); hl.setAttribute("x2", xs(best)); hl.setAttribute("visibility", "visible");
      tip.innerHTML = html;
      const yTop = Math.min(...opts.series.map((s) => (s.values[best] == null ? Infinity : ys(s.values[best]))));
      tip.style.left = `${(xs(best) / W) * rect.width}px`;
      tip.style.top = `${((isFinite(yTop) ? yTop : m.t) / H) * rect.height}px`;
      tip.classList.add("show");
    });
    overlay.addEventListener("mouseleave", () => { tip.classList.remove("show"); hl.setAttribute("visibility", "hidden"); });
  }
}

const legendItem = (color, label, kind = "") => `<span><i class="${kind}" style="--c:${color}"></i>${esc(label)}</span>`;
const miniStat = (label, value, foot, cls = "") => `<div class="card kpi ${cls}" style="box-shadow:none"><div class="kpi-label">${esc(label)}</div><div class="kpi-value">${value}</div><div class="kpi-foot">${foot}</div></div>`;

// ---------------------------------------------------------------- Data Fetching
async function load() {
  const btn = $("#refresh-btn");
  btn.classList.add("spinning");
  try {
    const res = await fetch("/api/dashboard", { cache: "no-store" });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    DATA = data;
    renderAll();
  } catch (e) {
    document.querySelector("main").insertAdjacentHTML("afterbegin",
      `<div class="error-box"><b>Could not load dashboard data.</b><br><span class="mono">${esc(e.message)}</span></div>`);
  } finally {
    btn.classList.remove("spinning");
  }
}

function renderAll() {
  if (!DATA) return;
  $("#source-line").textContent = `Updated ${DATA.timestamp} · SQLite & JSON Analytics`;
  const footerEl = $("#footer");
  if (footerEl) {
    const g = DATA.generated_from || {};
    footerEl.innerHTML = `Certificate Monitoring Dashboard · Database: <span class="mono">${esc(g.db || "certificate_logs.db")}</span> · Reference: <span class="mono">${esc(g.reference_file || "reference_set.xlsx")}</span>`;
  }
  renderOverview();
  renderDrift();
  renderQualityBaselines();
  renderAlerts();
  renderReview();
  renderReviewedTab();
}

// ---------------------------------------------------------------- OVERVIEW
function renderOverview() {
  const ov = DATA.overview;
  const kp = ov.kpis;

  $("#kpis").innerHTML = `
    ${miniStat("Production Requests", fmt.int(kp.total_production_requests), `Reference Set: ${kp.total_reference_requests} baseline`)}
    ${miniStat("Approved Verdicts", `${kp.approved_pct}%`, `${kp.approved_count} documents approved`, "good")}
    ${miniStat("Need Review", `${kp.need_review_pct}%`, `${kp.need_review_count} flagged for review`, kp.need_review_pct > 20 ? "warn" : "")}
    ${miniStat("Rejected Verdicts", `${kp.reject_pct}%`, `${kp.reject_count} rejected / non-ISO`, kp.reject_pct > 15 ? "bad" : "")}
    ${miniStat("Error Rate", `${kp.error_rate_pct}%`, `${kp.error_count} failed / unreadable`, kp.error_rate_pct > 5 ? "bad" : "good")}`;

  $("#lat-sub").textContent = `p95 Latency threshold: ${fmt.ms(ov.p95_threshold_ms)} · Avg Cost: ${fmt.usd(kp.avg_cost_usd)} (Total: ${fmt.usd(kp.total_cost_usd)})`;

  const ls = ov.latency_series;
  if (ls && ls.request_numbers && ls.request_numbers.length) {
    lineChart($("#chart-latency"), {
      aria: "Latency over requests",
      x: ls.request_numbers,
      series: [
        { name: "Latency", values: ls.latency_ms, color: "hsla(172, 72%, 52%, 0.35)", width: 1.5 },
        { name: "Rolling p95", values: ls.rolling_p95_ms, color: COLORS.accent, width: 2.6 },
      ],
      thresholds: [{ value: ov.p95_threshold_ms, label: `p95 Threshold (${fmt.ms(ov.p95_threshold_ms)})` }],
      yMin: 0,
      yFmt: (v) => fmt.ms(v),
      legend: legendItem("hsla(172, 72%, 52%, 0.35)", "Request Latency") + legendItem(COLORS.accent, "Rolling p95") + legendItem(COLORS.threshold, "p95 Threshold", "dash"),
      tooltip: (i) => `<b>Request #${ls.request_numbers[i]}</b><br>Latency: ${fmt.ms(ls.latency_ms[i])}<br>Rolling p95: ${fmt.ms(ls.rolling_p95_ms[i])}`,
    });
  }

  // Alert status
  const nCrit = kp.critical_alarms_count || 0;
  const nTot = kp.active_alarms_count || 0;
  const orbCls = nCrit > 0 ? "critical" : nTot > 0 ? "warning" : "ok";
  const orbTitle = nCrit > 0 ? `${nCrit} Critical Alarms Active` : nTot > 0 ? `${nTot} Alarms Triggered` : "All Pipeline Checks Normal";

  $("#alert-status").innerHTML = `
    <div class="card-head"><div><p class="card-title">System Status</p><p class="card-sub">${nTot} alarms recorded</p></div></div>
    <div class="status-hero">
      <div class="status-orb ${orbCls}"></div>
      <div><h4>${orbTitle}</h4><p class="dim" style="margin:2px 0 0;font-size:12px">Continuous Drift & Quality Alarms</p></div>
    </div>
    <div class="stat-pair">
      <div><span>Avg Confidence:</span> <b>${kp.avg_confidence_score} / 100</b></div>
      <div><span>p50 Latency:</span> <b>${fmt.ms(kp.p50_latency_ms)}</b></div>
      <div><span>p95 Latency:</span> <b>${fmt.ms(kp.p95_latency_ms)}</b></div>
    </div>`;

  // Windows Table
  const winRows = (ov.windows || []).map((w) => {
    const isRef = w.kind === "reference";
    return `
      <tr class="${isRef ? "is-ref" : ""}">
        <td>${isRef ? badge("ref", w.label, false) : `<b>${esc(w.label)}</b>`}</td>
        <td><span class="mono">${esc(w.request_range)}</span></td>
        <td class="num">${fmt.int(w.count)}</td>
        <td class="num">${fmt.pct(w.error_rate_pct)}</td>
        <td class="num">${fmt.ms(w.p95_latency_ms)}</td>
        <td>${(() => {
          const st = String(w.status || "STABLE").toUpperCase();
          const cls = st === "BASELINE" ? "ref" : (st === "DRIFT_ALERT" || st === "DRIFT_DETECTED" || st.includes("DRIFT") || st.includes("ALERT")) ? "critical" : "ok";
          return badge(cls, w.status || "STABLE");
        })()}</td>
      </tr>`;
  }).join("");

  $("#windows-table").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Window / Dataset</th>
          <th>Sample Range</th>
          <th class="num">Count</th>
          <th class="num">Error Rate</th>
          <th class="num">p95 Latency</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>${winRows}</tbody>
    </table>`;
}

// ---------------------------------------------------------------- DRIFT
function renderDrift() {
  const df = DATA.drift;
  const tests = df.tests || [];
  if (!tests.length) {
    $("#drift-check").innerHTML = `<div class="warn-note">No drift tests recorded yet. Run <code>python main.py 2</code> or mode 2 simulation to generate analytics/drift_test.json.</div>`;
    return;
  }

  const latest = tests[tests.length - 1];
  const hasDrift = df.has_drift;
  $("#drift-check").innerHTML = `
    <div class="check-strip ${hasDrift ? "bad" : "ok"}">
      <div class="icon">${hasDrift ? "!" : "✓"}</div>
      <div><b>${hasDrift ? "Drift Detected" : "Pipeline Inputs & Outputs Stable"}</b> — Evaluated across ${tests.length} production window(s). Latest: ${esc(latest.range_label)}.</div>
    </div>`;

  // Input drift charts (Page count for PDFs, Pixel size for images)
  const winLabels = ["Ref", ...tests.map((t) => t.range_label)];
  const inFeats = [
    {
      key: "pdf_pages",
      altKey: "page_count",
      label: "PDF Page Count",
      unit: "pages",
      getVal: (t, isRef) => {
        const sub = (t && t.input_drift && (t.input_drift.pdf_pages || t.input_drift.page_count)) || {};
        return isRef ? ((sub.reference && sub.reference.mean) ?? sub.ref_mean ?? sub.reference_mean ?? 0) : ((sub.recent && sub.recent.mean) ?? sub.recent_mean ?? 0);
      },
    },
    {
      key: "image_pixel_size",
      altKey: "image_pixel_size",
      label: "Image Pixel Size",
      unit: "pixels",
      getVal: (t, isRef) => {
        const sub = (t && t.input_drift && t.input_drift.image_pixel_size) || {};
        return isRef ? ((sub.reference && sub.reference.mean) ?? sub.ref_mean ?? sub.reference_mean ?? 0) : ((sub.recent && sub.recent.mean) ?? sub.recent_mean ?? 0);
      },
    },
  ];

  $("#input-charts").innerHTML = inFeats.map((f) => `
    <div class="card">
      <div class="card-head">
        <div><p class="card-title">${f.label} — Window Mean vs Frozen Reference</p><p class="card-sub">Monitors document complexity shifts</p></div>
      </div>
      <div class="chart" id="chart-in-${f.key}"></div>
    </div>`).join("");

  inFeats.forEach((f) => {
    const refMean = f.getVal(tests[0], true);
    const values = [refMean, ...tests.map((t) => f.getVal(t, false))];
    lineChart($(`#chart-in-${f.key}`), {
      aria: f.label,
      x: winLabels,
      categorical: true,
      firstIsRef: true,
      series: [{
        name: f.label,
        values,
        color: COLORS.accent,
        points: true,
        labels: true,
        labelFmt: (v) => f.unit === "pages" ? fmt.num(v, 2) : fmt.int(Math.round(v)),
        pointColor: (i) => (i === 0 ? COLORS.ref : COLORS.accent),
      }],
      refLine: { value: refMean, label: `ref ${f.unit === "pages" ? fmt.num(refMean, 2) : fmt.int(Math.round(refMean))}` },
      yMin: 0,
      yFmt: (v) => f.unit === "pages" ? fmt.num(v, 1) : fmt.int(Math.round(v)),
      tooltip: (i) => i === 0 ? `<b>Frozen Reference</b><br>Mean ${fmt.num(refMean, 2)} ${f.unit}` : `<b>${esc(tests[i - 1].range_label)}</b><br>Mean ${fmt.num(values[i], 2)} ${f.unit}`,
    });
  });

  // Output drift charts (Missing fields, Confidence score, Undefined site_iso_type)
  const outFeats = [
    {
      key: "missing_fields_per_valid_row",
      altKey: "missing_fields_avg",
      label: "Avg Missing Fields",
      sub: "Average missing fields per verified certificate",
      getVal: (t, isRef) => {
        const sub = (t && t.output_drift && (t.output_drift.missing_fields_per_valid_row || t.output_drift.missing_fields_avg)) || {};
        return isRef ? ((sub.reference && sub.reference.mean) ?? sub.ref_mean ?? sub.reference_value ?? 0) : ((sub.recent && sub.recent.mean) ?? sub.recent_mean ?? sub.recent_value ?? 0);
      },
      fmt: (v) => fmt.num(v, 2),
      yFmt: (v) => fmt.num(v, 1),
    },
    {
      key: "confidence_score",
      altKey: "confidence_score_avg",
      label: "Avg Confidence Score",
      sub: "Verification rule confidence score (/100)",
      getVal: (t, isRef) => {
        const sub = (t && t.output_drift && (t.output_drift.confidence_score || t.output_drift.confidence_score_avg)) || {};
        return isRef ? ((sub.reference && sub.reference.mean) ?? sub.ref_mean ?? sub.reference_value ?? 100) : ((sub.recent && sub.recent.mean) ?? sub.recent_mean ?? sub.recent_value ?? 100);
      },
      fmt: (v) => fmt.num(v, 1),
      yFmt: (v) => fmt.num(v, 0),
    },
    {
      key: "site_iso_type_undefined",
      altKey: "undefined_site_iso_type_ratio",
      label: "Undefined Site ISO Ratio (%)",
      sub: "Ratio of Undefined site classifications",
      getVal: (t, isRef) => {
        const sub = (t && t.output_drift && (t.output_drift.site_iso_type_undefined || t.output_drift.undefined_site_iso_type_ratio)) || {};
        if (isRef) {
          return sub.ref_rate_pct ?? (sub.ref_ratio != null ? sub.ref_ratio * 100 : sub.reference_value ?? 0);
        }
        return sub.recent_rate_pct ?? (sub.recent_ratio != null ? sub.recent_ratio * 100 : sub.recent_value ?? 0);
      },
      fmt: (v) => `${Number(v).toFixed(1)}%`,
      yFmt: (v) => `${Math.round(v)}%`,
    },
  ];

  $("#output-charts").innerHTML = outFeats.map((f) => `
    <div class="card">
      <div class="card-head">
        <div><p class="card-title">${f.label}</p><p class="card-sub">${f.sub}</p></div>
      </div>
      <div class="chart" id="chart-out-${f.key}"></div>
    </div>`).join("");

  outFeats.forEach((f) => {
    const refVal = f.getVal(tests[0], true);
    const values = [refVal, ...tests.map((t) => f.getVal(t, false))];
    lineChart($(`#chart-out-${f.key}`), {
      aria: f.label,
      x: winLabels,
      categorical: true,
      firstIsRef: true,
      series: [{
        name: f.label,
        values,
        color: COLORS.accent,
        points: true,
        labels: true,
        labelFmt: (v) => f.fmt(v),
        pointColor: (i) => (i === 0 ? COLORS.ref : COLORS.accent),
      }],
      refLine: { value: refVal, label: `ref ${f.fmt(refVal)}` },
      yFmt: f.yFmt,
      tooltip: (i) => i === 0 ? `<b>Frozen Reference</b><br>Value: ${f.fmt(refVal)}` : `<b>${esc(tests[i - 1].range_label)}</b><br>Observed: ${f.fmt(values[i])}`,
    });
  });

  // Share bars for verdict distribution
  if (shareTestIdx == null || shareTestIdx >= tests.length) shareTestIdx = tests.length - 1;
  $("#share-picker").innerHTML = tests.map((t, i) => `<button id="share-pick-${i}" data-i="${i}" class="${i === shareTestIdx ? "active" : ""}">${esc(t.range_label)}</button>`).join("");
  $("#share-picker").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { shareTestIdx = +b.dataset.i; renderDrift(); }));
  renderVerdictShareBars(tests[shareTestIdx]);

  // Quality Drift (Error Rate & Judge Pass Rate)
  const qFeats = [
    {
      key: "error_rate",
      label: "Pipeline Error Rate (%)",
      sub: "Extraction failure and unreadable document rate",
      getRef: (t) => {
        const q = (t && t.quality_drift && t.quality_drift.error_rate) || {};
        return q.ref_rate_pct ?? q.reference_rate_pct ?? 0;
      },
      getRecent: (t) => {
        const q = (t && t.quality_drift && t.quality_drift.error_rate) || {};
        return q.recent_rate_pct ?? q.error_rate_pct ?? 0;
      },
      getCount: (t, isRef) => {
        const q = (t && t.quality_drift && t.quality_drift.error_rate) || {};
        return isRef ? q.ref_count : q.recent_count;
      },
    },
    {
      key: "judge_pass_rate",
      label: "Online Judge Pass Rate (%)",
      sub: "Random online LLM judge pass rate on sampled certificates",
      getRef: (t) => {
        const q = (t && t.quality_drift && t.quality_drift.judge_pass_rate) || {};
        return q.ref_pass_rate_pct ?? q.reference_pass_rate_pct ?? 37.5;
      },
      getRecent: (t) => {
        const q = (t && t.quality_drift && t.quality_drift.judge_pass_rate) || {};
        if (q.recent_judged_count === 0 || q.recent_pass_rate_pct == null) {
          return null; // Return null when no judge sample taken in that window
        }
        return q.recent_pass_rate_pct;
      },
      getCount: (t, isRef) => {
        const q = (t && t.quality_drift && t.quality_drift.judge_pass_rate) || {};
        return isRef ? q.ref_judged_count : q.recent_judged_count;
      },
    },
  ];

  $("#rule-drift").innerHTML = qFeats.map((q) => `
    <div class="card">
      <div class="card-head">
        <div><p class="card-title">${q.label}</p><p class="card-sub">${q.sub}</p></div>
      </div>
      <div class="chart" id="chart-qd-${q.key}"></div>
    </div>`).join("");

  qFeats.forEach((q) => {
    const refV = q.getRef(tests[0]);
    const values = [refV, ...tests.map((t) => q.getRecent(t))];
    lineChart($(`#chart-qd-${q.key}`), {
      aria: q.label,
      x: winLabels,
      categorical: true,
      firstIsRef: true,
      series: [{
        name: q.label,
        values,
        color: COLORS.accent,
        points: true,
        labels: true,
        labelFmt: (v) => v != null ? fmt.pct(v, 1) : "—",
        pointColor: (i, v) => (i === 0 ? COLORS.ref : v == null ? COLORS.faint : v < (refV - 15) ? COLORS.critical : COLORS.accent),
      }],
      refLine: { value: refV, label: `ref ${fmt.pct(refV, 1)}` },
      yMin: 0,
      yMax: 105,
      yFmt: (v) => `${v}%`,
      tooltip: (i) => {
        if (i === 0) {
          const n = q.getCount(tests[0], true);
          return `<b>Frozen Reference</b><br>${q.label}: ${fmt.pct(refV)}` + (n != null ? `<br>Sample size: n=${n}` : "");
        }
        const val = values[i];
        const t = tests[i - 1];
        const n = q.getCount(t, false);
        if (val == null) {
          return `<b>${esc(t.range_label)}</b><br><span class="dim">No judge sample evaluated in this window (n=0)</span>`;
        }
        return `<b>${esc(t.range_label)}</b><br>${q.label}: <b>${fmt.pct(val)}</b>` + (n != null ? `<br>Sample size: n=${n}` : "");
      },
    });
  });
}

function renderVerdictShareBars(test) {
  const vd = (test && test.output_drift && test.output_drift.verdict_distribution) || {};
  const cats = vd.categories || {
    Approved: { ref_share_pct: 70, recent_share_pct: 65, pp_change: -5 },
    "Need Review": { ref_share_pct: 20, recent_share_pct: 25, pp_change: 5 },
    Reject: { ref_share_pct: 10, recent_share_pct: 10, pp_change: 0 },
  };

  const rows = Object.entries(cats).map(([name, data]) => `
    <div class="share-row" title="${esc(name)}: ref ${data.ref_share_pct}%, recent ${data.recent_share_pct}%">
      <span class="cat">${esc(name)}</span>
      <div class="bars">
        <div class="bar ref"><span style="width:${data.ref_share_pct}%"></span></div>
        <div class="bar recent"><span style="width:${data.recent_share_pct}%"></span></div>
      </div>
      <span class="pp ${Math.abs(data.pp_change || 0) >= 10 ? "big" : ""}">${fmt.pp(data.pp_change)}</span>
    </div>`).join("");

  $("#share-bars").innerHTML = `<div class="span-3"><p class="card-title" style="margin-bottom:8px">Verdict Proportions</p>${rows}</div>`;
}

// ---------------------------------------------------------------- GOLDEN DATASET BASELINES
function renderQualityBaselines() {
  const qb = DATA.quality_baselines;
  if (!qb || !qb.available) {
    $("#baseline-kpis").innerHTML = `<p class="muted">No baseline files found in analytics/. Run <code>python main.py 1</code> to validate golden regression.</p>`;
    return;
  }

  // Populate Dropdown Selectors
  const baseSelect = $("#baseline-select");
  const compSelect = $("#compare-select");

  if (baseSelect && baseSelect.options.length === 0) {
    baseSelect.innerHTML = qb.files.map((f) => `<option value="${f}" ${f === qb.active_file ? "selected" : ""}>${f}</option>`).join("");
    baseSelect.addEventListener("change", async () => {
      await switchBaseline(baseSelect.value, compSelect.value);
    });
  }

  if (compSelect && compSelect.options.length === 0) {
    compSelect.innerHTML = qb.files.map((f) => `<option value="${f}" ${f === qb.compare_file ? "selected" : ""}>${f}</option>`).join("");
    compSelect.addEventListener("change", async () => {
      await switchBaseline(baseSelect.value, compSelect.value);
    });
  }

  renderBaselineDetails(qb.active, qb.compare, qb.feature_comparison);
}

async function switchBaseline(activeFile, compareFile) {
  try {
    const res = await fetch(`/api/baseline?file=${encodeURIComponent(compareFile)}`);
    const data = await res.json();
    if (data.available) {
      DATA.quality_baselines = data;
      renderBaselineDetails(data.active, data.compare, data.feature_comparison);
    }
  } catch (e) {
    console.error("Failed to switch baseline:", e);
  }
}

function renderBaselineDetails(act, cmp, featureRows) {
  const b = act || {};
  const c = cmp || {};

  $("#baseline-kpis").innerHTML = `
    ${miniStat("Golden Pass Rate", fmt.pct(b.pass_rate_percent), `${b.passed_cases || 0} of ${b.total_cases || 0} passed (Compare: ${fmt.pct(c.pass_rate_percent)})`, b.pass_rate_percent >= 85 ? "good" : "warn")}
    ${miniStat("False Pass Rate", fmt.pct(b.false_pass_rate_total_percent), `${b.false_pass_cases || 0} safety failures (Compare: ${fmt.pct(c.false_pass_rate_total_percent)})`, b.false_pass_cases === 0 ? "good" : "bad")}
    ${miniStat("Average Total Score", `${fmt.num(b.average_total_score)} / 10`, `Avg row score: ${fmt.num(b.average_row_score * 100, 1)}%`)}
    ${miniStat("Golden Test Suite", esc(b.golden_file || "golden_dataset_regression.xlsx"), `Validated at ${esc(b.validation_timestamp || "")}`)}`;

  // Feature scores & metric comparison table
  const fRowsHtml = (featureRows || []).map((r) => {
    const diff = r.delta_score;
    const isFp = !!r.is_fp;
    const isCount = !!r.is_count;
    const isPct = !!r.is_pct;

    let diffText = "";
    if (isCount) {
      diffText = `${diff > 0 ? "+" : ""}${diff} case${Math.abs(diff) === 1 ? "" : "s"}`;
    } else if (isPct) {
      diffText = `${diff > 0 ? "+" : ""}${Number(diff).toFixed(1)} pp`;
    } else {
      diffText = `${diff > 0 ? "+" : ""}${(diff * 100).toFixed(1)}%`;
    }

    const badgeCls = diff === 0 ? "equal" : (isFp ? diff < 0 : diff > 0) ? "better" : "worse";
    const diffBadge = `<span class="diff-badge ${badgeCls}">${esc(diffText)}</span>`;

    const activeDisplay = isCount
      ? `${r.active_score} case${r.active_score === 1 ? "" : "s"}`
      : isPct
      ? `<b>${fmt.pct(r.active_accuracy_pct)}</b>`
      : `<b>${fmt.pct(r.active_accuracy_pct)}</b> <span class="dim">(${fmt.num(r.active_score, 3)})</span>`;

    const compareDisplay = isCount
      ? `${r.compare_score} case${r.compare_score === 1 ? "" : "s"}`
      : isPct
      ? `<b>${fmt.pct(r.compare_accuracy_pct)}</b>`
      : `<b>${fmt.pct(r.compare_accuracy_pct)}</b> <span class="dim">(${fmt.num(r.compare_score, 3)})</span>`;

    const isSummaryRow = isCount || isPct || r.key === "average_total_score";

    return `
      <tr style="${isSummaryRow ? "background:hsla(226,30%,14%,0.4);" : ""}">
        <td>
          <b>${esc(r.label)}</b>
          <div class="dim mono" style="font-size:11px">${esc(r.key)}</div>
        </td>
        <td>${activeDisplay}</td>
        <td>${compareDisplay}</td>
        <td>${diffBadge}</td>
      </tr>`;
  }).join("");

  $("#feature-comparison-table").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Feature / Field</th>
          <th>Active Baseline Accuracy</th>
          <th>Compare Baseline Accuracy</th>
          <th>Accuracy Shift</th>
        </tr>
      </thead>
      <tbody>${fRowsHtml}</tbody>
    </table>`;

  // Column breakdown
  const colBreakdown = b.column_breakdown || {};
  const colRows = Object.entries(colBreakdown).map(([k, v]) => `
    <tr>
      <td><b>${titleCase(k)}</b></td>
      <td class="num">${v.total_points} / ${v.max_possible_points}</td>
      <td class="num"><b>${fmt.pct(v.accuracy_percent)}</b></td>
      <td class="num">${fmt.pct(v.avg_similarity_percent)}</td>
    </tr>`).join("");

  $("#column-breakdown-table").innerHTML = `
    <table>
      <thead>
        <tr><th>Column</th><th class="num">Points</th><th class="num">Accuracy</th><th class="num">Similarity</th></tr>
      </thead>
      <tbody>${colRows}</tbody>
    </table>`;

  // False Pass Cases
  const fpCases = b.false_pass_urls || [];
  if (!fpCases.length) {
    $("#false-pass-container").innerHTML = `<div class="check-strip ok" style="margin:12px 0;"><div class="icon">✓</div><div><b>No False Pass Cases</b><br><span class="muted">All failed documents were accurately rejected.</span></div></div>`;
  } else {
    const fpItems = fpCases.map((f) => `
      <div style="background:hsla(350,85%,62%,0.08);border:1px solid hsla(350,85%,62%,0.25);border-radius:8px;padding:12px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <b style="color:var(--critical)">Case #${f.index} — Confidence: ${f.confidence_score}/100</b>
          <span class="badge critical">False Pass</span>
        </div>
        <div class="mono dim" style="font-size:11.5px;word-break:break-all;margin-bottom:4px;">${esc(f.url)}</div>
        <div style="font-size:12px;color:var(--text-2);"><b>Violation:</b> ${esc(f.verification_reviews)}</div>
      </div>`).join("");
    $("#false-pass-container").innerHTML = fpItems;
  }
}

// ---------------------------------------------------------------- ALERTS
function renderAlerts() {
  const al = DATA.alerts;
  const alarms = al.alarms || [];
  const pill = $("#alerts-pill");
  if (pill) {
    pill.hidden = !alarms.length;
    pill.textContent = alarms.length;
    if (al.critical_count > 0) pill.style.background = "var(--critical)";
  }

  const filtered = sevFilter === "all" ? alarms : alarms.filter((a) => a.severity === sevFilter);
  if (!filtered.length) {
    $("#alerts-table").innerHTML = `<p class="muted" style="padding:16px;">No alerts recorded${sevFilter === "all" ? "" : ` with severity ${sevFilter}`}.</p>`;
    return;
  }

  const rows = filtered.map((a) => `
    <tr>
      <td>${sevBadge(a.severity)}</td>
      <td class="mono"><b>${esc(a.range_str)}</b></td>
      <td><b>${esc(a.metric || a.category)}</b></td>
      <td class="mono over" style="color:var(--${a.severity})">${esc(a.recent_value ?? "—")}</td>
      <td class="mono dim">${esc(a.reference_value ?? a.threshold ?? "—")}</td>
      <td class="muted" style="max-width:400px">${esc(a.message)}</td>
      <td class="mono dim" style="font-size:11.5px">${esc((a.timestamp || "").slice(0, 19))}</td>
    </tr>`).join("");

  $("#alerts-table").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Severity</th>
          <th>Sample Range</th>
          <th>Metric / Test</th>
          <th>Observed</th>
          <th>Reference / Threshold</th>
          <th>Detail</th>
          <th>Time</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------------------------------------------------------------- REVIEW QUEUE
let reviewMode = "single";
let reviewFilter = "all";
let currentReviewCase = null;
let selectedDecision = null;
let currentReviewerId = localStorage.getItem("cert_reviewer_id") || "REV-01";
let lastSavedToast = null;

async function fetchNextReviewCase(targetRequestId = null) {
  try {
    const url = targetRequestId ? `/api/review-queue/next?request_id=${encodeURIComponent(targetRequestId)}` : "/api/review-queue/next";
    const res = await fetch(url);
    const data = await res.json();
    currentReviewCase = data.case;
    selectedDecision = null;
    renderReviewCountersBar();
    renderSingleCaseCard();
  } catch (e) {
    console.error("Failed to fetch review case:", e);
  }
}

function updateReviewPills(pendingCount, reviewedCount) {
  const rPill = $("#review-pill");
  if (rPill) {
    rPill.hidden = false;
    rPill.textContent = pendingCount;
    rPill.style.background = pendingCount > 0 ? "var(--warning)" : "var(--ok)";
  }
  const revPill = $("#reviewed-pill");
  if (revPill) {
    revPill.hidden = false;
    revPill.textContent = reviewedCount;
    revPill.style.background = "var(--ok)";
  }
}

function renderReviewCountersBar() {
  const bar = $("#review-counters-bar");
  if (!bar) return;
  const stats = (DATA && DATA.review_queue && DATA.review_queue.stats) || {};
  const p = stats.pending_count || 0;
  const r = stats.reviewed_count || 0;
  const tot = stats.target_queue_size || (p + r) || 1;
  const pct = Math.round((r / (p + r || 1)) * 100);

  updateReviewPills(p, r);

  bar.innerHTML = `
    <div class="review-counters-bar">
      <div class="counter-stats-group">
        <div class="counter-stat-item pending">
          <span class="val">${p}</span>
          <span class="lbl">Pending in 30% Queue</span>
        </div>
        <div class="counter-stat-item reviewed">
          <span class="val">${r}</span>
          <span class="lbl">Reviewed Cases</span>
        </div>
        <div class="counter-stat-item total">
          <span class="val">${stats.total_production || 0}</span>
          <span class="lbl">Total Prod Records</span>
        </div>
      </div>

      <div class="review-progress-wrap">
        <div class="progress-meta">
          <span>Human Review Progress</span>
          <b>${r} decisions saved (${pct}%)</b>
        </div>
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" style="width:${pct}%"></div>
        </div>
      </div>

      <div class="reviewer-profile-chip">
        <label for="reviewer-id-input">Reviewer ID</label>
        <input type="text" id="reviewer-id-input" value="${esc(currentReviewerId)}" placeholder="REV-01">
      </div>
    </div>`;

  const revIn = $("#reviewer-id-input");
  if (revIn) {
    revIn.addEventListener("change", (e) => {
      currentReviewerId = e.target.value.trim() || "REV-01";
      localStorage.setItem("cert_reviewer_id", currentReviewerId);
      const decRev = $("#decision-reviewer-id");
      if (decRev) decRev.value = currentReviewerId;
    });
  }
}

function renderSingleCaseCard() {
  const container = $("#review-single-container");
  if (!container) return;

  if (!currentReviewCase) {
    container.innerHTML = `
      <div class="review-complete-card">
        <div class="review-complete-glyph">🎉</div>
        <h3>Review Queue Completed</h3>
        <p>All items in the current 30% sample have been reviewed. New production runs will automatically queue further cases.</p>
        <button class="btn-save-decision" id="btn-goto-reviewed-tab">View Reviewed Cases Tab →</button>
      </div>`;
    const btnGoto = $("#btn-goto-reviewed-tab");
    if (btnGoto) btnGoto.addEventListener("click", () => showView("reviewed"));
    return;
  }

  const c = currentReviewCase;
  const tierCls = `p${c.priority_tier || 1}`;

  // Reasons list
  const reasonsHtml = (c.reasons || []).map((r) => `<span class="reason-tag urgent">${esc(r)}</span>`).join("");

  // Extracted fields for pre-filling
  const siteName = c.site_name || "";
  const address = c.address || "";
  const scope = c.scope || "";
  const isoType = c.iso_type || "";
  const issuedDate = c.issued_date || "";
  const expirationDate = c.expiration_date || "";
  const siteIsoType = c.site_iso_type || "";
  const certLanguage = c.certificate_language || "";

  // Judge reasoning display
  const judgeScore = c.judge_score;
  const judgeReasoning = c.judge_reasoning || "Not sampled in 15% random online judge audit.";
  const isJudged = judgeScore !== null && judgeScore !== undefined;

  container.innerHTML = `
    ${lastSavedToast ? `
      <div class="save-toast-banner" style="background:hsla(150,65%,52%,0.15);border:1px solid var(--ok);color:var(--ok);padding:12px 18px;border-radius:12px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;font-size:13.5px;font-weight:600;">
        <span>${esc(lastSavedToast)}</span>
        <button type="button" id="btn-close-toast" style="background:transparent;border:0;color:var(--ok);cursor:pointer;font-size:18px;font-weight:700;">✕</button>
      </div>
    ` : ""}

    <div class="case-review-card">
      <!-- Header -->
      <div class="case-header">
        <div class="case-title-group">
          <span class="prio-chip ${tierCls}">${esc(c.priority_label || "Prio")}</span>
          <span class="case-req-num">Request #${c.request_id}</span>
          <span class="badge info">${esc(c.doc_type || "pdf").toUpperCase()}</span>
          ${badge(c.verdict === "Approved" ? "ok" : c.verdict === "Reject" ? "critical" : "warning", `AI Verdict: ${c.verdict || "Need Review"}`)}
        </div>
        <div class="dim mono" style="font-size:12px">${esc(c.timestamp || "")}</div>
      </div>

      <!-- Reasons -->
      <div class="case-reasons-banner">
        <span class="reason-label">Queue Prioritization:</span>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">${reasonsHtml}</div>
      </div>

      <!-- Prominent URL & Open Button Bar -->
      <div style="padding:16px 24px;background:hsla(226,30%,12%,0.75);border-bottom:1px solid var(--border-strong);display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;">
        <div style="flex:1;min-width:280px;">
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);margin-bottom:3px;">Certificate Document URL</div>
          <div class="mono" style="font-size:13px;word-break:break-all;color:var(--text);">${esc(c.url)}</div>
        </div>
        <button type="button" class="btn-open-url" id="btn-open-cert-url">
          <span>Open Certificate in New Tab ↗</span>
        </button>
      </div>

      <!-- Sections Grid -->
      <div class="case-sections-grid">
        <!-- AI Extracted Fields Pane -->
        <div class="case-pane">
          <div class="pane-head">
            <h4 class="pane-title">AI Extracted Certificate Metadata</h4>
            <span class="badge neutral mono">Confidence: ${c.confidence_score ?? "—"} / 100</span>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
            <div class="kv-block">
              <span class="kv-lbl">Site / Company Name</span>
              <div class="kv-val">${esc(siteName || "—")}</div>
            </div>
            <div class="kv-block">
              <span class="kv-lbl">ISO / Standard Type</span>
              <div class="kv-val">${esc(isoType || "—")}</div>
            </div>
          </div>

          <div class="kv-block">
            <span class="kv-lbl">Address</span>
            <div class="kv-val">${esc(address || "—")}</div>
          </div>

          <div class="kv-block">
            <span class="kv-lbl">Scope of Certification</span>
            <div class="kv-val" style="max-height:90px;overflow-y:auto;">${esc(scope || "—")}</div>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;">
            <div class="kv-block">
              <span class="kv-lbl">Issued Date</span>
              <div class="kv-val mono">${esc(issuedDate || "—")}</div>
            </div>
            <div class="kv-block">
              <span class="kv-lbl">Expiration Date</span>
              <div class="kv-val mono">${esc(expirationDate || "—")}</div>
            </div>
            <div class="kv-block">
              <span class="kv-lbl">Language</span>
              <div class="kv-val">${esc(certLanguage || "—")}</div>
            </div>
          </div>

          <div class="kv-block">
            <span class="kv-lbl">Site ISO Classification</span>
            <div class="kv-val"><b>${esc(siteIsoType || "Undefined")}</b></div>
          </div>
        </div>

        <!-- Rule Verification & Judge Pane -->
        <div class="case-pane">
          <div class="pane-head">
            <h4 class="pane-title">Rule Verification & Judge</h4>
            <span class="badge ${c.verdict === "Approved" ? "ok" : c.verdict === "Reject" ? "critical" : "warning"}">${esc(c.verdict)}</span>
          </div>

          <div class="kv-block">
            <span class="kv-lbl">Verification Rule Checks & Violations</span>
            <div class="judge-note-box" style="font-style:normal;margin:0;">
              ${esc(c.checks_passed_or_failed || "None flagged.")}
            </div>
          </div>

          <div class="kv-block" style="margin-top:8px;">
            <div class="pane-head">
              <span class="kv-lbl">Online LLM Judge (15% Sample)</span>
              ${isJudged ? badge(judgeScore === 1 ? "ok" : "critical", judgeScore === 1 ? "Judge: Pass (1)" : "Judge: Fail (0)") : badge("neutral", "Not Sampled")}
            </div>
            <div class="judge-note-box ${!isJudged ? "unjudged-note" : ""}">
              <b>Reasoning:</b> ${esc(judgeReasoning)}
            </div>
          </div>

          <div class="stat-pair" style="margin-top:auto;padding-top:10px;border-top:1px solid var(--border);">
            <div><span>Pages:</span> <b>${c.page_count ?? "—"}</b></div>
            <div><span>Latency:</span> <b>${fmt.ms(c.total_latency_ms)}</b></div>
            <div><span>Cost:</span> <b>${fmt.usd(c.cost_usd)}</b></div>
          </div>
        </div>
      </div>

      <!-- Decision Controls Panel -->
      <div class="decision-panel">
        <h4 class="decision-panel-title">Decision Controls & Human Corrections</h4>

        <!-- 3 Verdict Buttons -->
        <div class="decision-buttons-group">
          <button type="button" class="btn-decision approved ${selectedDecision === "Approved" ? "active" : ""}" id="btn-dec-approved">
            <span class="dec-icon">✓</span> Approve Certificate
          </button>
          <button type="button" class="btn-decision need-review ${selectedDecision === "Need Review" ? "active" : ""}" id="btn-dec-needreview">
            <span class="dec-icon">⚠</span> Flag for Need Review
          </button>
          <button type="button" class="btn-decision reject ${selectedDecision === "Reject" ? "active" : ""}" id="btn-dec-reject">
            <span class="dec-icon">✗</span> Reject / Non-Compliant
          </button>
        </div>

        <!-- Editable Field Corrections Grid (Pre-populated) -->
        <div class="corrections-grid">
          <div class="form-field">
            <label for="corr-site-name">Corrected Site Name</label>
            <input type="text" id="corr-site-name" value="${esc(siteName)}" placeholder="Site / Company Name">
          </div>
          <div class="form-field">
            <label for="corr-iso-type">Corrected ISO Standard</label>
            <input type="text" id="corr-iso-type" value="${esc(isoType)}" placeholder="e.g. ISO 9001:2015">
          </div>
          <div class="form-field">
            <label for="corr-issued-date">Corrected Issued Date</label>
            <input type="text" id="corr-issued-date" value="${esc(issuedDate)}" placeholder="YYYY-MM-DD">
          </div>
          <div class="form-field">
            <label for="corr-exp-date">Corrected Expiration Date</label>
            <input type="text" id="corr-exp-date" value="${esc(expirationDate)}" placeholder="YYYY-MM-DD">
          </div>
          <div class="form-field">
            <label for="corr-site-iso-type">Site ISO Classification</label>
            <select id="corr-site-iso-type">
              ${["Main Site", "Multiple Sites", "Single Site", "Undefined"].map(
                (opt) => `<option value="${opt}" ${opt.toLowerCase() === (siteIsoType || "").toLowerCase() ? "selected" : ""}>${opt}</option>`
              ).join("")}
            </select>
          </div>
          <div class="form-field">
            <label for="corr-language">Certificate Language</label>
            <input type="text" id="corr-language" value="${esc(certLanguage)}" placeholder="e.g. English, German">
          </div>
          <div class="form-field" style="grid-column: span 2;">
            <label for="corr-address">Company Address</label>
            <input type="text" id="corr-address" value="${esc(address)}" placeholder="Full street address and country">
          </div>
        </div>

        <div class="form-field">
          <label for="corr-scope">Scope of Certification</label>
          <textarea id="corr-scope" placeholder="Full certification scope text...">${esc(scope)}</textarea>
        </div>

        <!-- Notes and Reviewer ID -->
        <div style="display:grid;grid-template-columns:2fr 1fr;gap:14px;">
          <div class="form-field">
            <label for="decision-notes">Reviewer Notes & Rationale</label>
            <textarea id="decision-notes" placeholder="Reason for verdict or notes on corrected fields..."></textarea>
          </div>
          <div class="form-field">
            <label for="decision-reviewer-id">Reviewer ID</label>
            <input type="text" id="decision-reviewer-id" value="${esc(currentReviewerId)}" placeholder="REV-01">
          </div>
        </div>

        <!-- Action Row -->
        <div class="decision-actions-row">
          <button type="button" class="btn-skip-case" id="btn-skip-case">Skip to Next Pending</button>
          <button type="button" class="btn-save-decision" id="btn-save-decision">
            Save Decision & Move to Reviewed Tab →
          </button>
        </div>
      </div>
    </div>`;

  // Attach Open URL button
  const btnOpen = $("#btn-open-cert-url");
  if (btnOpen) {
    btnOpen.addEventListener("click", () => {
      if (c.url) window.open(c.url, "_blank");
    });
  }

  // Close toast
  const btnCloseToast = $("#btn-close-toast");
  if (btnCloseToast) {
    btnCloseToast.addEventListener("click", () => {
      lastSavedToast = null;
      const b = $(".save-toast-banner");
      if (b) b.remove();
    });
  }

  // Decision buttons
  const decBtns = {
    Approved: $("#btn-dec-approved"),
    "Need Review": $("#btn-dec-needreview"),
    Reject: $("#btn-dec-reject"),
  };

  Object.entries(decBtns).forEach(([k, btn]) => {
    if (!btn) return;
    btn.addEventListener("click", () => {
      selectedDecision = k;
      Object.values(decBtns).forEach((b) => b && b.classList.remove("active"));
      btn.classList.add("active");
    });
  });

  // Save decision handler
  const btnSave = $("#btn-save-decision");
  if (btnSave) {
    btnSave.addEventListener("click", async () => {
      if (!selectedDecision) {
        alert("Please select a decision verdict: Approve Certificate, Flag for Need Review, or Reject.");
        return;
      }

      const revId = ($("#decision-reviewer-id") ? $("#decision-reviewer-id").value.trim() : "") || currentReviewerId;
      currentReviewerId = revId;
      localStorage.setItem("cert_reviewer_id", revId);

      const notes = $("#decision-notes") ? $("#decision-notes").value.trim() : "";

      // Collect corrections
      const corrections = {
        site_name: $("#corr-site-name") ? $("#corr-site-name").value.trim() : "",
        iso_type: $("#corr-iso-type") ? $("#corr-iso-type").value.trim() : "",
        issued_date: $("#corr-issued-date") ? $("#corr-issued-date").value.trim() : "",
        expiration_date: $("#corr-exp-date") ? $("#corr-exp-date").value.trim() : "",
        site_iso_type: $("#corr-site-iso-type") ? $("#corr-site-iso-type").value : "",
        certificate_language: $("#corr-language") ? $("#corr-language").value.trim() : "",
        address: $("#corr-address") ? $("#corr-address").value.trim() : "",
        scope: $("#corr-scope") ? $("#corr-scope").value.trim() : "",
      };

      btnSave.disabled = true;
      btnSave.textContent = "Saving decision…";

      try {
        const payload = {
          request_id: c.request_id,
          decision: selectedDecision,
          reviewer_notes: notes || `Marked as ${selectedDecision} via human review`,
          reviewer_id: revId,
          corrected_fields: corrections,
        };

        const res = await fetch("/api/review-queue/decision", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        const resData = await res.json();
        if (resData.success) {
          lastSavedToast = `✓ Request #${c.request_id} recorded as "${selectedDecision}". Saved to reviewed/human_review.json. Automatically promoted next candidate into 30% queue.`;
          currentReviewCase = resData.next_case;
          if (resData.stats) {
            DATA.review_queue.stats = resData.stats;
          }
          selectedDecision = null;
          renderReviewCountersBar();
          renderSingleCaseCard();
          // Also refresh reviewed tab cache
          refreshReviewedCases();
        } else {
          alert(`Error: ${resData.error || "Failed to save decision."}`);
          btnSave.disabled = false;
          btnSave.textContent = "Save Decision & Move to Reviewed Tab →";
        }
      } catch (err) {
        alert(`Request failed: ${err.message}`);
        btnSave.disabled = false;
        btnSave.textContent = "Save Decision & Move to Reviewed Tab →";
      }
    });
  }

  // Skip handler
  const btnSkip = $("#btn-skip-case");
  if (btnSkip) {
    btnSkip.addEventListener("click", () => {
      lastSavedToast = null;
      fetchNextReviewCase();
    });
  }
}

function renderReviewTable() {
  const rq = (DATA && DATA.review_queue) || {};
  const items = rq.items || [];
  const card = $("#review-table-card");
  if (!card) return;

  let filtered = items;
  if (reviewFilter === "p1") filtered = items.filter((x) => x.priority_tier === 1);
  else if (reviewFilter === "p2") filtered = items.filter((x) => x.priority_tier === 2);
  else if (reviewFilter === "p3") filtered = items.filter((x) => x.priority_tier === 3);
  else if (reviewFilter === "p4") filtered = items.filter((x) => x.priority_tier === 4);
  else if (reviewFilter === "p5") filtered = items.filter((x) => x.priority_tier === 5);

  if (!filtered.length) {
    card.innerHTML = `<p class="muted" style="padding:24px;text-align:center;">No pending queue cases matching filter <span class="mono">${esc(reviewFilter)}</span>.</p>`;
    return;
  }

  const rows = filtered.map((it) => {
    const tierCls = `p${it.priority_tier || 1}`;
    return `
      <tr>
        <td><span class="prio-chip ${tierCls}">${esc(it.priority_label || "Prio")}</span></td>
        <td class="mono"><b>Req #${it.request_id}</b></td>
        <td>
          <a href="${esc(it.url)}" target="_blank" class="mono" style="color:var(--accent);text-decoration:none;font-size:12px;" title="${esc(it.url)}">
            ${esc(String(it.url).slice(0, 48))}… ↗
          </a>
        </td>
        <td>${badge(it.verdict === "Approved" ? "ok" : it.verdict === "Reject" ? "critical" : "warning", it.verdict || "Need Review")}</td>
        <td class="num mono">${it.confidence_score ?? "—"}/100</td>
        <td><span class="dim" style="font-size:12px;">${esc((it.reasons || []).join("; "))}</span></td>
        <td>
          <button class="btn-action agree" data-load-case="${it.request_id}" style="font-size:11px;padding:4px 8px;">Review Case</button>
        </td>
      </tr>`;
  }).join("");

  card.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Priority</th>
            <th>Request #</th>
            <th>Certificate URL</th>
            <th>Verdict</th>
            <th class="num">Confidence</th>
            <th>Queue Trigger</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  card.querySelectorAll("[data-load-case]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const rid = btn.dataset.loadCase;
      switchReviewMode("single");
      fetchNextReviewCase(rid);
    });
  });
}

function switchReviewMode(mode) {
  reviewMode = mode;
  const btnSingle = $("#btn-mode-single");
  const btnTable = $("#btn-mode-table");
  const singleContainer = $("#review-single-container");
  const tableContainer = $("#review-table-container");

  if (btnSingle) btnSingle.classList.toggle("active", mode === "single");
  if (btnTable) btnTable.classList.toggle("active", mode === "table");

  if (mode === "single") {
    if (singleContainer) singleContainer.style.display = "block";
    if (tableContainer) tableContainer.style.display = "none";
    if (!currentReviewCase) fetchNextReviewCase();
  } else {
    if (singleContainer) singleContainer.style.display = "none";
    if (tableContainer) tableContainer.style.display = "block";
    renderReviewTable();
  }
}

function renderReview() {
  renderReviewCountersBar();
  if (reviewMode === "single") {
    if (!currentReviewCase) fetchNextReviewCase();
    else renderSingleCaseCard();
  } else {
    renderReviewTable();
  }
}

// ---------------------------------------------------------------- REVIEWED CASES TAB
async function refreshReviewedCases() {
  try {
    const res = await fetch("/api/reviewed");
    const data = await res.json();
    if (DATA) DATA.reviewed_cases = data.items || [];
    renderReviewedTab();
  } catch (e) {
    console.error("Failed to refresh reviewed cases:", e);
  }
}

function renderReviewedTab() {
  const container = $("#reviewed-table-container");
  const kpiEl = $("#reviewed-kpis");
  if (!container || !DATA) return;

  const items = DATA.reviewed_cases || [];

  const nApproved = items.filter((x) => (x.decision && x.decision.reviewer_verdict) === "Approved").length;
  const nNeedRev = items.filter((x) => (x.decision && x.decision.reviewer_verdict) === "Need Review").length;
  const nReject = items.filter((x) => (x.decision && x.decision.reviewer_verdict) === "Reject").length;
  const nCorrected = items.filter((x) => x.decision && x.decision.corrected_fields && Object.keys(x.decision.corrected_fields).length > 0).length;

  if (kpiEl) {
    kpiEl.innerHTML = `
      ${miniStat("Total Reviewed Decisions", fmt.int(items.length), "Recorded in reviewed/human_review.json")}
      ${miniStat("Approved by Reviewer", fmt.int(nApproved), `${items.length ? ((nApproved / items.length) * 100).toFixed(1) : 0}% of decisions`, "good")}
      ${miniStat("Need Review / Escalated", fmt.int(nNeedRev), `${items.length ? ((nNeedRev / items.length) * 100).toFixed(1) : 0}% of decisions`, "warn")}
      ${miniStat("Rejected by Reviewer", fmt.int(nReject), `${items.length ? ((nReject / items.length) * 100).toFixed(1) : 0}% of decisions (${nCorrected} with field corrections)`, "bad")}`;
  }

  if (!items.length) {
    container.innerHTML = `<p class="muted" style="padding:24px;text-align:center;">No completed human reviews yet. Complete reviews in the <b>Review Queue</b> tab to record decisions here.</p>`;
    return;
  }

  const rows = items.map((it) => {
    const orig = it.original_data || {};
    const dec = it.decision || {};
    const corr = dec.corrected_fields || {};
    const corrCount = Object.keys(corr).length;
    const corrTags = Object.entries(corr).map(([k, v]) => `<span class="correction-tag" title="Field: ${k}"><b>${esc(k)}:</b> ${esc(String(v).slice(0, 25))}</span>`).join(" ");

    return `
      <tr>
        <td>
          <div class="mono" style="font-weight:700;">Req #${it.request_id}</div>
          <div class="dim mono" style="font-size:11px;">${esc(it.timestamp || "")}</div>
        </td>
        <td>
          <a href="${esc(it.url)}" target="_blank" class="mono" style="color:var(--accent);text-decoration:none;font-size:12px;">
            ${esc(String(it.url).slice(0, 42))}… ↗
          </a>
        </td>
        <td>${badge(orig.verdict === "Approved" ? "ok" : orig.verdict === "Reject" ? "critical" : "warning", orig.verdict || "Need Review")}</td>
        <td>${badge(dec.reviewer_verdict === "Approved" ? "ok" : dec.reviewer_verdict === "Reject" ? "critical" : "warning", dec.reviewer_verdict || "Done")}</td>
        <td style="max-width:320px;">
          ${corrCount > 0 ? `<div>${corrTags}</div>` : `<span class="dim">No corrections</span>`}
        </td>
        <td>
          <div style="font-size:12px;color:var(--text);">${esc(dec.reviewer_notes || "—")}</div>
          <div class="dim mono" style="font-size:11px;">Reviewer: ${esc(dec.reviewer_id || "REV-01")}</div>
        </td>
      </tr>`;
  }).join("");

  container.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Request / Time</th>
          <th>Certificate URL</th>
          <th>Original AI Verdict</th>
          <th>Human Verdict</th>
          <th>Corrected Fields</th>
          <th>Notes & Reviewer</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------------------------------------------------------------- TAB NAVIGATION
function showView(name) {
  currentView = name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  history.replaceState(null, "", `#${name}`);
  requestAnimationFrame(() => document.querySelectorAll(`#view-${name} .chart`).forEach((c) => c._opts && lineChart(c, c._opts)));

  if (name === "review") {
    renderReview();
  } else if (name === "reviewed") {
    refreshReviewedCases();
  }
}

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
$("#refresh-btn").addEventListener("click", load);
const refBtnRev = $("#refresh-reviewed-btn");
if (refBtnRev) refBtnRev.addEventListener("click", refreshReviewedCases);

$("#sev-filter").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
  sevFilter = b.dataset.sev;
  $("#sev-filter").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  renderAlerts();
}));

const rqViewPicker = $("#rq-view-picker");
if (rqViewPicker) {
  rqViewPicker.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    switchReviewMode(b.dataset.mode);
  }));
}

const rqPicker = $("#review-filter");
if (rqPicker) {
  rqPicker.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    reviewFilter = b.dataset.filter;
    rqPicker.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    renderReview();
  }));
}

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => document.querySelectorAll(".view.active .chart").forEach((c) => c._opts && lineChart(c, c._opts)), 150);
});

const initial = location.hash.replace("#", "");
if (["overview", "drift", "quality", "alerts", "review", "reviewed"].includes(initial)) {
  showView(initial);
}
load();
