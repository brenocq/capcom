// Hand-written SVG charts. Colors are CSS variables, so a theme switch restyles
// them; values come from the API only.

import { h, s, showTip, hideTip, tipOn, usd, usdShort, two, hm, monthDay, SERIES } from './lib.js';

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
  return h('div.xaxis', { style: `margin-left:${left}px` }, ticks.map((tk, i) => {
    const f = pos(tk.t);
    const shift = f < 0.03 ? '0' : f > 0.97 ? '-100%' : '-50%';
    return h('span', { style: `left:${(f * 100).toFixed(2)}%;transform:translateX(${shift})`, text: tk.label });
  }));
}

// ------------------------------------------------------------------ band chart

// Cumulative lines, largest first, each with a band down to the next line, filled
// in its own color with a vertical fade; the total on top in the text color.
// opts: {times, members: [{label, values, color}], total, start, end, now,
//        height, left, timeLabel(t)}
export function bandChart(opts) {
  const { members, total } = opts;
  const times = [opts.start, ...opts.times];
  const lines = members.map((m) => [0, ...m.values]);
  const tot = [0, ...total];
  const x0 = opts.start, x1 = Math.max(opts.end, times[times.length - 1]);
  const W = 1000, H = opts.height || 300, left = opts.left || 52;
  const ticks = niceTicks(Math.max(...tot, 1e-6));
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

// ------------------------------------------------------------------ per-day bars

// days: [{date (Date), values, total}], members: [{label, color}]
export function dayBars(days, members, todayIso) {
  const max = Math.max(...days.map((d) => d.total), 1e-9);
  const many = days.length > 31;
  const bars = h('div.days', { style: many ? 'gap:2px' : '' }, days.map((d) => {
    const bar = h('div.bar', { style: `height:${(d.total / max * 85).toFixed(1)}%` },
      d.values.map((v, j) => (v > 0 ? h('div', { style: `flex:${v} 1 0px;background:${members[j].color}` }) : null)));
    const col = h('div.day', null, many ? null : h('span.amt', { text: d.total > 0 ? usdShort(d.total) : '' }), bar);
    return tipOn(col, `${monthDay(d.date)}: ${usd(d.total)}`, () => members
      .map((m, j) => ({ label: m.label, value: usd(d.values[j]), color: m.color }))
      .filter((r, j) => d.values[j] > 0).reverse());
  }));
  const step = many ? 7 : days.length > 14 ? 3 : 1;
  const labs = h('div.daylabs', { style: many ? 'gap:2px' : '' }, days.map((d, i) => {
    const today = d.iso === todayIso;
    const text = i % step === 0 || today ? (step === 1 ? 'SMTWTFS'[d.date.getDay()] + ' ' + d.date.getDate() : monthDay(d.date)) : '';
    return h('span', { class: today ? 'today' : null, text, style: step > 1 ? 'overflow:visible' : null });
  }));
  return h('div', null, bars, labs);
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
        `${weekdays[r]} ${two(hr)}:00 to ${two((hr + 1) % 24)}:00`, [{ label: 'Spent', value: usd(v) }])))),
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
