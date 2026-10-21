// Limits: the subscription's 5-hour and weekly windows. The Overview's two cards
// (the reading, the forecast fan) and the Limits page (each window's sawtooth over
// time, and what spent each window by project). Data from /api/limits.

import { h, s, api, usd, seg, empty, setQuery, hm, monthDay, dayName, showTip, hideTip, SERIES, OTHER_COLOR } from './lib.js';
import { timeTicks, xAxis } from './charts.js';

let uid = 0;
const DAY_OPTIONS = [[7, '7 days'], [14, '14 days'], [30, '30 days'], [90, '90 days']];

const pctText = (x, digits = 0) => `${(+x).toFixed(digits)}%`;
const when = (iso, weekly) => {
  const d = new Date(iso);
  return weekly ? `${dayName(d).slice(0, 3)} ${hm(d)}` : hm(d);
};
const isWeekly = (w) => w.window === 'seven_day';

function notRecording(lim) {
  return empty('Not recording yet', h('span', null, 'Run ', h('code', { text: lim.install || 'capcom limits --install' }),
    ' to see the change to your status line. capcom then records the 5-hour and weekly percentages each time they move, and forecasts the value at reset.'));
}

function ring(percent, warn) {
  const r = 24, c = 2 * Math.PI * r;
  const f = Math.max(0, Math.min(1, (percent || 0) / 100));
  return s('svg', { width: 60, height: 60, viewBox: '0 0 60 60', 'aria-hidden': 'true' },
    s('circle', { cx: 30, cy: 30, r, fill: 'none', 'stroke-width': 6, style: 'stroke:var(--subtle)' }),
    f > 0 ? s('circle', {
      cx: 30, cy: 30, r, fill: 'none', 'stroke-width': 6, 'stroke-linecap': 'round', transform: 'rotate(-90 30 30)',
      'stroke-dasharray': `${(f * c).toFixed(2)} ${c.toFixed(2)}`, style: `stroke:${warn ? 'var(--accent-strong)' : 'var(--accent)'}`,
    }) : null);
}

// What the cards and the page say about a window, in words.
function summary(w) {
  const out = [];
  const weekly = isWeekly(w);
  const fc = w.forecast;
  if (fc) {
    out.push(`About ${pctText(fc.now)} now (${pctText(fc.reading)} at the last reading). At the reset, ${pctText(fc.median)}; 80% range ${pctText(fc.low)} to ${pctText(fc.high)}.`);
    if (fc.runs_out_at) out.push(`At this pace it runs out around ${when(fc.runs_out_at, weekly)}.`);
  } else if (w.reading && !w.current) {
    out.push(`The last reading was ${pctText(w.reading.percent)} at ${when(w.reading.time, true)}; that window has reset since.`);
  } else if (w.reason) {
    out.push(`No forecast yet: ${w.reason} (${w.samples} recorded).`);
  }
  return out;
}

function fitNote(w) {
  const f = w.fit;
  if (!f) return null;
  return `Fit: ${f.percent_per_usd.toFixed(3)}% per API-equivalent dollar, ${f.note}; mean error ${f.mean_abs_error.toFixed(1)} points.`;
}

// ------------------------------------------------------------------ chart

// Usage windows over time: each window's readings as a rising line that drops to
// 0 at its reset, a dashed line at 100%, and the current window's forecast as a
// fan (the 80% range, filled with a fade, and the median dashed).
// opts: {segments: [{points, resets_at, reset, hit_limit}], fan, now, x0, x1, height, left, weekly}
export function limitChart(opts) {
  const W = 1000, H = opts.height || 160, left = opts.left || 40;
  const { x0, x1 } = opts;
  const fan = opts.fan || [];
  let top = 100;
  for (const p of fan) top = Math.max(top, p[3]);
  const ymax = top > 100 ? Math.ceil(top / 25) * 25 : 100;
  const X = (t) => ((t - x0) / (x1 - x0) * W).toFixed(1);
  const Y = (v) => (H - v / ymax * H).toFixed(1);
  const id = 'fan' + (++uid);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true', style: 'overflow:hidden' },
    s('defs', null, s('linearGradient', { id, x1: 0, y1: 0, x2: 0, y2: 1 },
      s('stop', { offset: 0, style: 'stop-color:var(--accent);stop-opacity:0.34' }),
      s('stop', { offset: 1, style: 'stop-color:var(--accent);stop-opacity:0.06' }))));
  const hover = [];
  const stroke = { fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' };
  for (const sg of opts.segments) {
    const pts = sg.points;
    const endT = sg.resets_at ? new Date(sg.resets_at).getTime() : pts.length ? pts[pts.length - 1][0] : 0;
    if (!pts.length || endT < x0) continue;
    let d = pts.map((p, i) => (i ? 'L' : 'M') + X(p[0]) + ' ' + Y(p[1])).join('');
    const last = pts[pts.length - 1];
    if (sg.reset && sg.resets_at) {
      const rt = new Date(sg.resets_at).getTime();
      d += 'L' + X(rt) + ' ' + Y(last[1]);
      svg.append(s('path', { d: 'M' + X(rt) + ' ' + Y(last[1]) + 'L' + X(rt) + ' ' + Y(0), ...stroke, 'stroke-width': 1, 'stroke-dasharray': '3 3', style: 'stroke:var(--muted)' }));
    }
    svg.append(s('path', { d, ...stroke, style: 'stroke:var(--text)' }));
    for (const p of pts) hover.push({ t: p[0], v: p[1], hit: sg.hit_limit && p[1] >= 100 });
  }
  if (fan.length > 1) {
    let band = fan.map((p, i) => (i ? 'L' : 'M') + X(p[0]) + ' ' + Y(p[3])).join('');
    for (let i = fan.length - 1; i >= 0; i--) band += 'L' + X(fan[i][0]) + ' ' + Y(fan[i][2]);
    svg.append(s('path', { d: band + 'Z', fill: `url(#${id})` }));
    svg.append(s('path', { d: fan.map((p, i) => (i ? 'L' : 'M') + X(p[0]) + ' ' + Y(p[1])).join(''), ...stroke, 'stroke-dasharray': '6 4', style: 'stroke:var(--accent)' }));
    const segs = opts.segments;
    const cur = segs.length ? segs[segs.length - 1] : null;
    if (cur && cur.points.length) {
      const lp = cur.points[cur.points.length - 1];
      svg.append(s('path', { d: `M${X(lp[0])} ${Y(lp[1])}L${X(fan[0][0])} ${Y(fan[0][1])}`, ...stroke, 'stroke-dasharray': '2 3', style: 'stroke:var(--text)' }));
    }
  }
  const plot = h('div.chart', { style: `height:${H}px;margin:12px 0 0 ${left}px` });
  for (const v of ymax > 100 ? [0, 50, 100, ymax] : [0, 50, 100]) {
    const t = ((1 - v / ymax) * 100).toFixed(2) + '%';
    plot.append(h('div.gridline', { style: `top:${t}` + (v === 100 ? ';border-top:1px dashed var(--accent)' : '') }),
      h('span.ylab', { style: `left:-${left}px;top:${t}`, text: `${v}%` }));
  }
  plot.append(svg);
  for (const p of hover) {
    if (!p.hit) continue;
    plot.append(h('span.enddot', { style: `left:${((p.t - x0) / (x1 - x0) * 100).toFixed(2)}%;top:${((1 - p.v / ymax) * 100).toFixed(2)}%;background:var(--accent-strong)`, title: 'Rate limit hit' }));
  }
  if (opts.now && opts.now < x1 && opts.now > x0) {
    const f = ((opts.now - x0) / (x1 - x0) * 100).toFixed(2) + '%';
    plot.append(h('div.nowline', { style: `left:${f}` }), h('span.nowlab', { style: `left:${f}`, text: 'now' }));
  }
  const label = (t) => `${opts.weekly ? monthDay(new Date(t)) + ' ' : ''}${hm(new Date(t))}`;
  plot.addEventListener('mousemove', (e) => {
    const r = plot.getBoundingClientRect();
    const t = x0 + (e.clientX - r.left) / r.width * (x1 - x0);
    const rows = [];
    let best = null;
    for (const p of hover) if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    if (best && Math.abs(best.t - t) < (x1 - x0) / 40) rows.push({ label: `Reading at ${label(best.t)}`, value: pctText(best.v), color: 'var(--text)' });
    if (fan.length > 1 && t >= fan[0][0]) {
      let q = fan[0];
      for (const p of fan) if (p[0] <= t) q = p;
      rows.push({ label: `Forecast by ${label(q[0])}`, value: `${pctText(q[1])} (${pctText(q[2])}–${pctText(q[3])})`, color: 'var(--accent)' });
    }
    if (rows.length) showTip(e, label(t), rows); else hideTip();
  });
  plot.addEventListener('mouseleave', hideTip);
  return h('div', null, plot, xAxis(timeTicks(x0, x1, 6), x0, x1, left));
}

// ------------------------------------------------------------------ overview cards

export function limitCard(label, w, lim) {
  const head = (pct, big, small, warn) => h('div', { style: 'display:flex;align-items:center;gap:16px' }, ring(pct, warn),
    h('div', { style: 'display:flex;flex-direction:column;gap:2px' }, h('span.label', { text: label }),
      h('span.big', { style: big ? null : 'color:var(--muted)' }, big || 'No data yet', small ? h('small', { text: ' ' + small }) : null)));
  if (!lim || !lim.available || !w || !w.reading) {
    return h('section.card', null, head(0, null, null, false), notRecording(lim || {}));
  }
  const fc = w.forecast;
  const weekly = isWeekly(w);
  const now = fc ? fc.now : w.current ? w.reading.percent : null;
  const resets = w.reading.resets_at;
  const small = w.current && resets ? `resets ${when(resets, weekly)}` : null;
  const warn = fc && fc.high >= 100;
  const card = h('section.card', null, head(now ?? 0, now === null ? 'Reset' : pctText(now), small, warn));
  for (const line of summary(w)) card.append(h('span.note', { text: line }));
  if (w.current && w.history.length && resets) {
    const x0 = new Date(w.start).getTime(), x1 = new Date(resets).getTime();
    card.append(limitChart({ segments: [{ points: w.history, reset: false }], fan: fc ? fc.fan : [], now: fc ? fc.fan[0][0] : null, x0, x1, height: 110, weekly }));
  }
  const fit = fitNote(w);
  if (fit) card.append(h('span.note', { text: fit }));
  if (!lim.recording) card.append(notRecording(lim));
  card.append(h('a.note', { href: '/limits', text: 'Every window, and what spent it →' }));
  return card;
}

// ------------------------------------------------------------------ page

function windowSection(w, now, days) {
  const weekly = isWeekly(w);
  const card = h('section.card', null, h('div.split', null, h('h2', { text: w.label[0].toUpperCase() + w.label.slice(1) }),
    h('span.note', { text: `${w.samples} sample${w.samples === 1 ? '' : 's'}` })));
  if (!w.reading) { card.append(empty(null, 'No readings of this window yet.')); return card; }
  for (const line of summary(w)) card.append(h('p.note', { text: line }));
  const segs = [...w.segments].reverse(); // oldest first
  const fc = w.forecast;
  let first = Infinity;
  for (const sg of segs) if (sg.points.length) first = Math.min(first, sg.points[0][0]);
  const x0 = Math.max(now - days * 864e5, Math.min(first, now - (weekly ? 7 : 1) * 864e5));
  const end = fc ? new Date(fc.resets_at).getTime() : now;
  card.append(limitChart({
    segments: segs,
    fan: fc ? fc.fan : [], now, x0, x1: Math.max(end, now), height: 180, weekly: true,
  }));
  const fit = fitNote(w);
  if (fit) card.append(h('p.note', { text: fit }));
  if (w.fit && w.fit.method === 'per_type' && w.fit.weights.length) {
    card.append(h('div.blist', null, w.fit.weights.slice(0, 8).map((x) => h('div.bitem', null, h('div.top', null,
      h('span', { text: `${x.model.replace(/^claude-/, '')} ${x.category.replace('_', ' ')}` }),
      h('span.mono', { text: `${x.percent_per_mtok.toFixed(3)}% per M tokens` }))))));
  }
  // what spent each window, newest first: one stacked bar per window, a color per project
  const spent = w.segments.filter((sg) => sg.by_project && sg.by_project.length).slice(0, weekly ? 3 : 6);
  if (spent.length) {
    const colors = new Map();
    const colorOf = (name) => {
      if (name === '(other)') return OTHER_COLOR;
      if (!colors.has(name)) colors.set(name, SERIES[colors.size % SERIES.length]);
      return colors.get(name);
    };
    const rows = h('div.blist');
    for (const sg of spent) {
      const from = sg.start ? `${monthDay(new Date(sg.start))} ${hm(new Date(sg.start))}` : '';
      const to = sg.reset && sg.resets_at ? hm(new Date(sg.resets_at)) : 'now';
      const total = sg.usd || 1e-9;
      rows.append(h('div.bitem', null,
        h('div.top', null, h('span', { text: `${from} → ${to} · peak ${pctText(sg.peak)}${sg.hit_limit ? ' · limit hit' : ''}` }), h('span.mono', { text: usd(sg.usd) })),
        h('div.stackbar', { 'aria-hidden': 'true' }, sg.by_project.map((p) => h('span', { style: `width:${(p.usd / total * 100).toFixed(2)}%;background:${colorOf(p.project)}` }))),
        h('span.note', { text: sg.by_project.map((p) => `${p.project} ${usd(p.usd)}${p.percent === null ? '' : ` (≈${pctText(p.percent, 1)})`}`).join(' · ') })));
    }
    card.append(h('h3', { text: 'What spent each window' }), rows);
  }
  return card;
}

export async function renderLimits(main, r) {
  const days = DAY_OPTIONS.map((o) => o[0]).includes(+r.query.get('days')) ? +r.query.get('days') : 14;
  const d = await api(`limits?days=${days}`);
  main.append(h('header.head', null,
    h('div.titles', null, h('h1', { text: 'Limits' }),
      h('p.sub', { text: 'The 5-hour and weekly windows of your plan: how full, when they reset, and what filled them.' })),
    seg('Window', DAY_OPTIONS, days, (v) => setQuery({ days: v === 14 ? null : v }))));
  if (!d.available) {
    main.append(h('section.card', null, notRecording(d)));
    return;
  }
  if (!d.recording) main.append(h('section.card', null, notRecording(d)));
  const now = Date.now();
  for (const key of ['five_hour', 'seven_day']) {
    const w = d.windows[key];
    if (w) main.append(windowSection(w, now, days));
  }
  main.append(h('p.note', { text: 'Percentages come from Claude Code\'s status line. Usage from other machines or claude.ai counts toward the limits but is not in these logs, so the fit treats it as error.' }));
}
