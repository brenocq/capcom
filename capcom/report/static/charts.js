// Hand-written SVG charts. Colors are CSS variables, so a theme switch restyles
// them; values come from the API only.

import { h, s, showTip, hideTip, tipOn, usd, usdShort, two, hm, monthDay, SERIES, maxOf } from './lib.js';

let uid = 0;

// ------------------------------------------------------------------ scales

export function niceTicks(max, count = 3) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => st >= raw) || 10 * mag;
  const ticks = [];
  for (let v = 0; v <= max + step * 0.999; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

// Local-time ticks between two epoch-ms times.
export function timeTicks(x0, x1, maxTicks = 7) {
  const span = x1 - x0, hour = 3600e3;
  const out = [];
  if (span <= 36 * hour) {
    const step = [1, 2, 3, 4, 6, 12].find((k) => span / (k * hour) <= maxTicks) || 12;
    const d = new Date(x0); d.setMinutes(0, 0, 0);
    while (d.getTime() < x0) d.setHours(d.getHours() + 1);
    while (d.getHours() % step) d.setHours(d.getHours() + 1);
    for (; d.getTime() <= x1; d.setHours(d.getHours() + step)) out.push({ t: d.getTime(), label: `${two(d.getHours())}:00` });
  } else {
    const days = span / (24 * hour);
    const step = [1, 2, 3, 7, 14, 30].find((k) => days / k <= maxTicks) || 30;
    const d = new Date(x0); d.setHours(0, 0, 0, 0);
    if (d.getTime() < x0) d.setDate(d.getDate() + 1);
    let i = 0;
    for (; d.getTime() <= x1; d.setDate(d.getDate() + 1), i++) if (i % step === 0) out.push({ t: d.getTime(), label: monthDay(d) });
  }
  return out;
}

function nearest(times, t) {
  let lo = 0, hi = times.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (times[mid] <= t) lo = mid; else hi = mid; }
  return Math.abs(times[hi] - t) < Math.abs(times[lo] - t) ? hi : lo;
}

function gradient(id, color, top, bottom) {
  return s('linearGradient', { id, x1: 0, y1: 0, x2: 0, y2: 1 },
    s('stop', { offset: 0, style: `stop-color:${color};stop-opacity:${top}` }),
    s('stop', { offset: 1, style: `stop-color:${color};stop-opacity:${bottom}` }));
}

function yGrid(plot, ticks, ymax, left, fmt) {
  for (const v of ticks) {
    const top = ((1 - v / ymax) * 100).toFixed(2) + '%';
    plot.append(h('div.gridline', { style: `top:${top}` }), h('span.ylab', { style: `left:-${left}px;top:${top}`, text: fmt(v) }));
  }
}

export function xAxis(ticks, x0, x1, left, xOf) {
  const pos = xOf || ((t) => (t - x0) / (x1 - x0));
  return thinned(h('div.xaxis', { style: `margin-left:${left}px` }, ticks.map((tk, i) => {
    const f = pos(tk.t);
    const shift = f < 0.03 ? '0' : f > 0.97 ? '-100%' : '-50%';
    return h('span', { style: `left:${(f * 100).toFixed(2)}%;transform:translateX(${shift})`, text: tk.label });
  })));
}

// Axis labels that would run into each other when the chart is narrow: whenever the
// row's width changes, every label is shown again and then, left to right, each one
// too close to the last one kept is hidden (a `.today` label is always kept).
const thinner = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver((entries) => entries.forEach((e) => thin(e.target)));

export function thinned(row) {
  if (thinner) thinner.observe(row);
  return row;
}

function thin(row) {
  const labels = [...row.children].filter((el) => el.textContent);
  labels.forEach((el) => { el.style.visibility = ''; });
  const range = document.createRange();
  const boxes = labels.map((el) => { range.selectNodeContents(el); return range.getBoundingClientRect(); });
  const kept = labels.map((el) => el.classList.contains('today'));
  const clear = (i) => kept.every((k, j) => !k || j === i || boxes[i].right + 6 <= boxes[j].left || boxes[j].right + 6 <= boxes[i].left);
  labels.forEach((el, i) => { if (!kept[i] && clear(i)) kept[i] = true; });
  labels.forEach((el, i) => { if (!kept[i]) el.style.visibility = 'hidden'; });
}

// ------------------------------------------------------------------ band chart

// Cumulative lines, largest first, each with a band down to the next line, filled
// in its own color with a vertical fade; the total on top in the text color, with
// a gray band down to the largest series and its amount labeled at its right end.
// opts: {times, members: [{label, values, color}], total, start, end, now,
//        height, left, timeLabel(t), marks: [{n, t, href, aria}]}
export function bandChart(opts) {
  const { members, total } = opts;
  const times = [opts.start, ...opts.times];
  const lines = members.map((m) => [0, ...m.values]);
  const tot = [0, ...total];
  const x0 = opts.start, x1 = Math.max(opts.end, times[times.length - 1]);
  const W = 1000, H = opts.height || 300, left = opts.left || 52;
  const ticks = niceTicks(maxOf(tot, 1e-6));
  const ymax = ticks[ticks.length - 1];
  const X = (t) => ((t - x0) / (x1 - x0) * W).toFixed(1);
  const Y = (v) => (H - v / ymax * H).toFixed(1);
  const lineOf = (vals) => vals.map((v, i) => (i ? 'L' : 'M') + X(times[i]) + ' ' + Y(v)).join('');
  const bandOf = (up, lo) => {
    let d = lineOf(up);
    for (let i = times.length - 1; i >= 0; i--) d += 'L' + X(times[i]) + ' ' + Y(lo ? lo[i] : 0);
    return d + 'Z';
  };
  const id = 'band' + (++uid);
  const defs = s('defs', null, gradient(`${id}-t`, 'var(--text)', 0.18, 0.02),
    members.map((m, i) => gradient(`${id}-${i}`, m.color, 0.32, 0.04)));
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' }, defs);
  svg.append(s('path', { d: bandOf(tot, lines[0]), fill: `url(#${id}-t)` }));
  lines.forEach((ln, i) => svg.append(s('path', { d: bandOf(ln, lines[i + 1]), fill: `url(#${id}-${i})` })));
  for (let i = lines.length - 1; i >= 0; i--) {
    svg.append(s('path', { d: lineOf(lines[i]), fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke', style: `stroke:${members[i].color}` }));
  }
  svg.append(s('path', { d: lineOf(tot), fill: 'none', 'stroke-width': 2.5, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--text)' }));

  const plot = h('div.chart', { style: `height:${H}px;margin:12px 0 0 ${left}px` });
  yGrid(plot, ticks, ymax, left, (v) => usd(v, v >= 100 || Number.isInteger(v) ? 0 : 2));
  plot.append(svg);
  if (opts.now && opts.now < x1) {
    const f = ((opts.now - x0) / (x1 - x0) * 100).toFixed(2) + '%';
    plot.append(h('div.nowline', { style: `left:${f}` }), h('span.nowlab', { style: `left:${f}`, text: 'now' }));
  }
  // spike markers: numbered, on the total line at the spike's start, linking to /spikes
  const placed = [];
  for (const m of opts.marks || []) {
    if (m.t < x0 || m.t > x1) continue;
    let i = times.findIndex((tm) => tm >= m.t);
    if (i < 0) i = times.length - 1;
    placed.push({ fx: (m.t - x0) / (x1 - x0), fy: 1 - tot[i] / ymax });
    const left = (placed[placed.length - 1].fx * 100).toFixed(2) + '%';
    const top = (placed[placed.length - 1].fy * 100).toFixed(2) + '%';
    plot.append(h('a.spikemark', { href: m.href || '/spikes', style: `left:${left};top:${top}`, 'aria-label': m.aria || `Spike ${m.n}`, title: m.aria || '', text: String(m.n) }));
  }
  // the total at the total line's end: a dot and the amount, inside the plot and
  // clear of the markers (above the line, below it near a marker, beside the end at the very top)
  const end = tot[tot.length - 1];
  if (end > 0) {
    const fx = (times[times.length - 1] - x0) / (x1 - x0), fy = 1 - end / ymax;
    const near = placed.some((p) => fx - p.fx < 0.16 && fx - p.fx > -0.04);
    const place = near ? '.below' : fy < 0.1 ? '.beside' : '';
    const pos = `left:${(fx * 100).toFixed(2)}%;top:${(fy * 100).toFixed(2)}%`;
    const lab = h('span.endlab' + place + (fx < 0.15 ? '.leftward' : ''), { style: pos, text: usd(end, end >= 100 ? 0 : 2) });
    plot.append(h('span.enddot', { style: pos }), lab);
    // once laid out, move the label clear of the spike markers and the "now" label
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(() => clearOfMarks(plot, lab, fx, fy, left));
      ro.observe(plot);
    }
  }
  const guide = h('div.guide', { hidden: true });
  plot.append(guide);
  const label = opts.timeLabel || ((t) => `${monthDay(new Date(t))} ${hm(new Date(t))}`);
  plot.addEventListener('mousemove', (e) => {
    const r = plot.getBoundingClientRect();
    const t = x0 + (e.clientX - r.left) / r.width * (x1 - x0);
    const i = nearest(times, t);
    guide.hidden = false;
    guide.style.left = ((times[i] - x0) / (x1 - x0) * 100).toFixed(2) + '%';
    showTip(e, i === 0 ? label(times[0]) : 'Up to ' + label(times[i]), [
      { label: 'Total', value: usd(tot[i]), color: 'var(--text)' },
      ...members.map((m, j) => ({ label: m.label, value: usd(lines[j][i]), color: m.color })),
    ]);
  });
  plot.addEventListener('mouseleave', () => { guide.hidden = true; hideTip(); });
  const xt = timeTicks(x0, x1);
  return h('div', null, plot, xAxis(xt, x0, x1, left));
}

// Where the total's label goes beside the end of the total line (`pt`, px in the
// plot): the first of a few offsets whose box overlaps no obstacle (the spike
// markers, the "now" label; boxes in the same px) and stays inside `box`, else the
// one that overlaps least. size: {w, h}. Returns the label's top-left offset from pt.
const END_OFFSETS = (w, hh) => [
  [-w - 6, -hh - 6], // above the end, to its left
  [-w - 10, -hh / 2], // beside it
  [-w - 8, 8], // below it
  [-w - 8, 30], // further below
  [-w - 8, 54],
  [8, -hh - 6], // to the right (a line ending near the left edge)
  [10, 8],
];

export function placeEndLabel(pt, size, obstacles, box) {
  let best = null;
  for (const [dx, dy] of END_OFFSETS(size.w, size.h)) {
    const r = { left: pt.x + dx, top: pt.y + dy, right: pt.x + dx + size.w, bottom: pt.y + dy + size.h };
    const outside = r.left < box.left || r.right > box.right || r.top < box.top || r.bottom > box.bottom;
    let overlap = 0;
    for (const o of obstacles) {
      const ox = Math.min(r.right, o.right + 2) - Math.max(r.left, o.left - 2);
      const oy = Math.min(r.bottom, o.bottom + 2) - Math.max(r.top, o.top - 2);
      if (ox > 0 && oy > 0) overlap += ox * oy;
    }
    const cost = overlap + (outside ? 1e6 : 0);
    if (cost === 0) return { dx, dy };
    if (!best || cost < best.cost) best = { dx, dy, cost };
  }
  return { dx: best.dx, dy: best.dy };
}

function clearOfMarks(plot, lab, fx, fy, axis) {
  const box = plot.getBoundingClientRect();
  if (!box.width) return;
  const rel = (r) => ({ left: r.left - box.left, top: r.top - box.top, right: r.right - box.left, bottom: r.bottom - box.top });
  const obstacles = [...plot.querySelectorAll('.spikemark, .nowlab')].map((m) => rel(m.getBoundingClientRect()));
  const pt = { x: fx * box.width, y: fy * box.height };
  const { dx, dy } = placeEndLabel(pt, { w: lab.offsetWidth, h: lab.offsetHeight }, obstacles,
    { left: -axis, top: -24, right: box.width + 4, bottom: box.height + 4 });
  lab.className = 'endlab';
  lab.style.transform = `translate(${dx}px, ${dy}px)`;
}

// ------------------------------------------------------------------ per-day bars

// days: [{date (Date), iso, values, total}], members: [{label, color}]; each day
// links to its own page
export function dayBars(days, members, todayIso) {
  const max = Math.max(...days.map((d) => d.total), 1e-9);
  const many = days.length > 31;
  // the amount shows above the highest day and today only; every day's is on hover
  const top = days.reduce((a, d, i) => (d.total > days[a].total ? i : a), 0);
  const bars = h('div.days.hl', { style: many ? 'gap:2px' : '' }, days.map((d, i) => {
    const bar = h('div.bar', { style: `height:${(d.total / max * 85).toFixed(1)}%` },
      d.values.map((v, j) => (v > 0 ? h('div', { style: `flex:${v} 1 0px;background:${members[j].color}` }) : null)));
    const shown = !many && d.total > 0 && (i === top || i === days.length - 1);
    const col = h('a.day', { href: `/day/${d.iso}` }, many ? null : h('span.amt', { text: shown ? usdShort(d.total) : '' }), bar);
    const rows = () => members
      .map((m, j) => ({ label: m.label, value: usd(d.values[j]), color: m.color }))
      .filter((r, j) => d.values[j] > 0).reverse();
    return tipOn(col, `${monthDay(d.date)}: ${usd(d.total)}`, rows, { native: true, aria: `${monthDay(d.date)}: ${usd(d.total)}, open the day` });
  }));
  const step = many ? 7 : days.length > 14 ? 3 : 1;
  // a regular label too close to today's (always shown, at the end) would run into it
  const clear = (i) => days.length - 1 - i >= Math.ceil(step * 0.8);
  const labs = thinned(h('div.daylabs', { style: many ? 'gap:2px' : '' }, days.map((d, i) => {
    const today = d.iso === todayIso;
    const text = (i % step === 0 && clear(i)) || today ? (step === 1 ? 'SMTWTFS'[d.date.getDay()] + ' ' + d.date.getDate() : monthDay(d.date)) : '';
    // a label wider than its bar runs to the right; the last one runs left, inside the chart
    const last = i === days.length - 1 ? ';display:flex;justify-content:flex-end' : '';
    return h('span', { class: today ? 'today' : null, text, style: step > 1 ? 'overflow:visible' + last : null });
  })));
  // a few days keep bars wide enough to tap, scrolling inside the card when narrow
  return h('div.daychart', { style: many ? null : `--days:${days.length}` }, bars, labs);
}

// ------------------------------------------------------------------ heatmap

const STOPS = {
  light: [[0, [240, 238, 230]], [0.35, [235, 195, 174]], [0.7, [217, 119, 87]], [1, [126, 58, 33]]],
  dark: [[0, [61, 61, 58]], [0.35, [126, 58, 33]], [0.7, [217, 119, 87]], [1, [242, 183, 155]]],
};

export function heatColor(x) {
  const stops = STOPS[document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'];
  const t = Math.sqrt(Math.max(0, Math.min(1, x)));
  for (let i = 1; i < stops.length; i++) {
    if (t <= stops[i][0]) {
      const [t0, c0] = stops[i - 1], [t1, c1] = stops[i];
      const f = (t - t0) / (t1 - t0);
      return 'rgb(' + c0.map((c, k) => Math.round(c + (c1[k] - c) * f)).join(',') + ')';
    }
  }
  return 'rgb(' + stops[3][1].join(',') + ')';
}

export function heatScale() {
  return h('span.scale', null, 'less', [0, 0.12, 0.49, 1].map((x) => h('i', { style: `background:${heatColor(x)}` })), 'more');
}

// cells[weekday Mon..Sun][hour 0..23] in USD
export function heatmap(cells, max, weekdays) {
  return h('div.scroll', null, h('div.heat', null,
    cells.map((row, r) => h('div.hr', null, h('span', { text: weekdays[r] }),
      row.map((v, hr) => tipOn(h('span.cell', { style: `background:${v > 0 ? heatColor(v / max) : 'var(--subtle)'}` }),
        `${weekdays[r]} ${two(hr)}:00 to ${two((hr + 1) % 24)}:00`, [{ label: 'Spent', value: usd(v) }], { focus: false })))),
    h('div.hx', null, ['00', '06', '12', '18'].map((t) => h('span', { text: t })))));
}

// ------------------------------------------------------------------ sparkline

export function sparkline(values, width = 96, height = 32) {
  const max = Math.max(...values, 1e-9);
  const n = Math.max(values.length - 1, 1);
  const d = values.map((v, i) => (i ? 'L' : 'M') + (i / n * 100).toFixed(1) + ' ' + (28 - v / max * 26).toFixed(1)).join('');
  return s('svg', { width, height, viewBox: '0 0 100 30', preserveAspectRatio: 'none', 'aria-hidden': 'true' },
    s('path', { d, fill: 'none', 'stroke-width': 1.5, 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--accent)' }));
}

// ------------------------------------------------------------------ compressed time axis

// A time-to-fraction map over [start, end] where each gap longer than `minGap`
// shrinks to `gapShare` of the width, so active time fills the chart.
export function activeAxis(start, end, gaps, minGap = 30 * 60e3, gapShare = 0.012) {
  const cut = gaps.filter((g) => g.end - g.start >= minGap && g.start >= start && g.end <= end);
  const removed = cut.reduce((a, g) => a + (g.end - g.start), 0);
  const active = Math.max(end - start - removed, 1);
  const activeShare = Math.max(1 - gapShare * cut.length, 0.3);
  const perGap = cut.length ? (1 - activeShare) / cut.length : 0;
  const f = (t) => {
    let acc = 0, prev = start;
    for (const g of cut) {
      if (t < g.start) break;
      acc += (g.start - prev) / active * activeShare;
      if (t < g.end) return acc + (t - g.start) / (g.end - g.start) * perGap;
      acc += perGap; prev = g.end;
    }
    return Math.min(1, acc + (Math.max(t, prev) - prev) / active * activeShare);
  };
  f.gaps = cut;
  return f;
}

export { SERIES };
