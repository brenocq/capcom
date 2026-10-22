// Limits: the subscription's 5-hour and weekly windows. The Overview's two cards
// (the reading, the forecast fan) and the Limits page (each window's sawtooth over
// time, and what spent each window by project). Data from /api/limits.

import { h, s, api, usd, tokens, seg, empty, setQuery, hm, monthDay, dayName, showTip, hideTip, SERIES, OTHER_COLOR } from './lib.js';
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
    ' in a terminal: it shows the one-line change to your status line and makes it if you say yes. capcom then records the 5-hour and weekly percentages each time they move, and forecasts the value at reset.'));
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

// When the forecast runs out, with its range (as `runs_out_text` in capcom/limits/view.py).
export function runsOut(fc, at) {
  const mid = fc.runs_out_at, early = fc.runs_out_early, late = fc.runs_out_late;
  const t = (iso) => new Date(iso).getTime();
  if (mid && late && early && t(early) < t(late)) return `At this pace it runs out between ${at(early)} and ${at(late)}, most likely ${at(mid)}.`;
  if (mid && late) return `At this pace it runs out around ${at(mid)}.`;
  if (mid) return `At this pace it runs out around ${at(mid)}${early && t(early) < t(mid) ? `, as early as ${at(early)}` : ''}; at a slower pace it lasts until the reset.`;
  if (early) return `It may run out before the reset, as early as ${at(early)} if the pace picks up.`;
  return null;
}

// Where a chart marks the forecast running out: null unless its most likely
// crossing of 100 % falls inside [x0, x1] (the reset). `f` is the crossing's place
// across the plot (0 to 1), `side` where the label goes (left of the marker past the
// middle, so it never runs off the right edge), `band` the earliest-latest range
// as fractions clipped to the plot, or null (the chart shows that range as the
// fan's own crossing of 100 %, not as a separate band).
export function runoutMark(fc, x0, x1, weekly) {
  if (!fc || !fc.runs_out_at || !(x1 > x0)) return null;
  const t = (iso) => new Date(iso).getTime();
  const mid = t(fc.runs_out_at);
  if (!(mid >= x0 && mid <= x1)) return null;
  const at = (iso) => when(iso, weekly);
  const frac = (v) => Math.max(0, Math.min(1, (v - x0) / (x1 - x0)));
  const early = fc.runs_out_early ? t(fc.runs_out_early) : null;
  const late = fc.runs_out_late ? t(fc.runs_out_late) : null;
  let sub = null, band = null;
  if (early !== null && late !== null && early < late) {
    sub = `between ${at(fc.runs_out_early)} and ${at(fc.runs_out_late)}`;
    band = [frac(early), frac(late)];
  } else if (early !== null && early < mid) {
    sub = `as early as ${at(fc.runs_out_early)}`;
    band = [frac(early), frac(mid)];
  }
  const f = frac(mid);
  return { t: mid, f, side: f > 0.5 ? 'left' : 'right', title: `Runs out ~${at(fc.runs_out_at)}`, sub, band };
}

// Where the run-out label goes: below the 100 % line, beside the guide, in the
// room the rising forecast leaves under itself. Pure: `f` is the crossing (0 to 1
// across the plot), `fan` the forecast ([t, median, low, high], rising), `plot`
// {x0, x1, w, h, ymax} in px and percent, `box` the label's size in px. Right of
// the guide unless the label would pass the right edge; then left of it. Its top
// sits under the lowest point of the fan and median over the label's span (and
// under the run-out dot). Returns {side, top (px from the plot's top), x: [a, b] px}.
export const LABEL_BOX = { w: 240, h: 46 }, LABEL_GAP = 10, PLOT_GUESS = 560;
export function runoutLabel(f, fan, plot, box = LABEL_BOX, reads = []) {
  const { x0, x1, w, h, ymax } = plot;
  const xc = f * w;
  const Ypx = (v) => h - v / ymax * h;
  const tAt = (x) => x0 + x / w * (x1 - x0);
  const place = (side) => {
    const a = side === 'right' ? xc + LABEL_GAP : xc - LABEL_GAP - box.w;
    const b = a + box.w;
    // lowest value of the fan's lower edge and median over [a, b]: at its points
    // inside the span and where the span's edges cut it
    let low = 100;
    const ta = tAt(a), tb = tAt(b);
    for (let i = 0; i < fan.length; i++) {
      const p = fan[i];
      if (p[0] >= ta && p[0] <= tb) low = Math.min(low, p[1], p[2]);
      const q = fan[i + 1];
      if (!q) continue;
      for (const t of [ta, tb]) {
        if (t >= p[0] && t <= q[0] && q[0] > p[0]) {
          const k = (t - p[0]) / (q[0] - p[0]);
          low = Math.min(low, p[1] + (q[1] - p[1]) * k, p[2] + (q[2] - p[2]) * k);
        }
      }
    }
    const top = Math.max(Ypx(100) + 10, Ypx(low) + 6);
    // the readings in the span must stay below the label, not run through it
    let high = -Infinity;
    for (const [t, v] of reads) if (t >= ta && t <= tb) high = Math.max(high, v);
    const floor = high === -Infinity ? h : Math.min(h, Ypx(high) - 6);
    return { side, top, x: [a, b], fits: top + box.h <= floor, inside: a >= 0 && b <= w };
  };
  const first = xc + LABEL_GAP + box.w <= w ? 'right' : 'left';
  let best = place(first);
  if (!best.fits) {
    const other = place(first === 'right' ? 'left' : 'right');
    if (other.fits && other.inside) best = other;
  }
  // clear: nothing passes through it; otherwise the label gets a solid backing
  return { side: best.side, top: Math.min(best.top, h - box.h), x: best.x, clear: best.fits };
}

// What the cards and the page say about a window, in words.
function summary(w) {
  const out = [];
  const weekly = isWeekly(w);
  const fc = w.forecast;
  if (fc) {
    // usage stops at the limit: past 100 % the paths only say when it gets there
    const cap = (x) => (x >= 100 ? 'the limit' : pctText(x));
    const head = `About ${pctText(fc.now)} now (${pctText(fc.reading)} at the last reading).`;
    out.push(fc.low >= 100 ? `${head} At this pace it reaches the limit before the reset.`
      : `${head} At the reset, ${fc.median >= 100 ? '100% (the limit)' : pctText(fc.median)}; 80% range ${pctText(fc.low)} to ${cap(fc.high)}.`);
    if (fc.rough) out.push(`Rough: ${fc.readings} reading${fc.readings === 1 ? '' : 's'} so far, so the range is wide; it narrows as readings come.`);
    const run = runsOut(fc, (iso) => when(iso, weekly));
    if (run) out.push(run);
  } else if (w.reading && !w.current) {
    out.push(`The last reading was ${pctText(w.reading.percent)} at ${when(w.reading.time, true)}; that window has reset since.`);
  } else if (w.reason) {
    out.push(`No forecast yet: ${w.reason} (${w.samples} recorded).`);
  }
  return out;
}

function fitNote(w) {
  return w.fit ? w.fit.note : null;
}

// ------------------------------------------------------------------ chart

// Usage windows over time: each window's readings as a rising line that drops to
// 0 at its reset, a dashed line at 100%, and the current window's forecast as a
// fan (the 80% range, filled, with hairline edges, and the median dashed).
// Readings are drawn as the line alone (Breno found the dots crowded); only a reading
// that hit the limit gets a mark. The stretch from a window's reset (0 %) to its first
// reading is dashed and labeled, since nothing was read there.
// opts: {segments: [{points, resets_at, reset, hit_limit, start_known}], fan, now, x0, x1,
//        height, left, weekly, since (ms: when recording began), runout (runoutMark)}
export function limitChart(opts) {
  const W = 1000, left = opts.left || 40;
  const { x0, x1 } = opts;
  const fan = opts.fan || [];
  const runout = opts.runout || null;
  // headroom above the limit when the fan crosses it: the fan and its median go on
  // past 100 % along the forecast, clipped only by the plot's edges, so where the
  // fan crosses the limit line shows the earliest-latest run-out. With a run-out
  // mark the headroom is 48 px, so the rise past the limit stays in view.
  const H = (opts.height || 160) + (runout ? 30 : 0);
  let top = 100;
  for (const p of fan) top = Math.max(top, p[3]);
  const ymax = runout ? 100 * H / (H - 48) : top > 100 ? 120 : 100;
  const X = (t) => ((t - x0) / (x1 - x0) * W).toFixed(1);
  const Y = (v) => (H - v / ymax * H).toFixed(1);
  const id = 'fan' + (++uid);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true', style: 'overflow:hidden' },
    s('defs', null,
      s('clipPath', { id: id + 'c' }, s('rect', { x: 0, y: 0, width: W, height: H }))));
  const hover = [];
  const stroke = { fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' };
  const gaps = [];
  for (const sg of opts.segments) {
    const pts = sg.points;
    const endT = sg.resets_at ? new Date(sg.resets_at).getTime() : pts.length ? pts[pts.length - 1][0] : 0;
    if (!pts.length || endT < x0) continue;
    // the window's reset at 0 % is not a reading: dashed up to the first one
    const unseen = sg.start_known && pts.length > 1 ? 1 : 0;
    if (unseen) {
      svg.append(s('path', { d: `M${X(pts[0][0])} ${Y(pts[0][1])}L${X(pts[1][0])} ${Y(pts[1][1])}`, ...stroke, 'stroke-dasharray': '4 4', style: 'stroke:var(--muted)' }));
      gaps.push({ a: pts[0], b: pts[1], label: opts.since && pts[0][0] < opts.since ? 'before recording' : 'not seen' });
    }
    const seen = pts.slice(unseen);
    let d = seen.map((p, i) => (i ? 'L' : 'M') + X(p[0]) + ' ' + Y(p[1])).join('');
    const last = pts[pts.length - 1];
    if (sg.reset && sg.resets_at) {
      const rt = new Date(sg.resets_at).getTime();
      d += 'L' + X(rt) + ' ' + Y(last[1]);
      svg.append(s('path', { d: 'M' + X(rt) + ' ' + Y(last[1]) + 'L' + X(rt) + ' ' + Y(0), ...stroke, 'stroke-width': 1, 'stroke-dasharray': '3 3', style: 'stroke:var(--muted)' }));
    }
    if (seen.length > 1 || (sg.reset && sg.resets_at)) svg.append(s('path', { d, ...stroke, style: 'stroke:var(--text)' }));
    for (const p of seen) hover.push({ t: p[0], v: p[1], hit: sg.hit_limit && p[1] >= 100 });
  }
  if (fan.length > 1) {
    let band = fan.map((p, i) => (i ? 'L' : 'M') + X(p[0]) + ' ' + Y(p[3])).join('');
    for (let i = fan.length - 1; i >= 0; i--) band += 'L' + X(fan[i][0]) + ' ' + Y(fan[i][2]);
    // clipped to the plot rectangle only: never flattened at the limit line
    const edge = (k) => fan.map((p, i) => (i ? 'L' : 'M') + X(p[0]) + ' ' + Y(p[k])).join('');
    const hair = { fill: 'none', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--accent);stroke-opacity:0.4' };
    svg.append(s('g', { 'clip-path': `url(#${id}c)` },
      s('path', { d: band + 'Z', style: 'fill:var(--accent);fill-opacity:var(--fan-fill)' }),
      s('path', { d: edge(2), ...hair }), s('path', { d: edge(3), ...hair }),
      s('path', { d: fan.map((p, i) => (i ? 'L' : 'M') + X(p[0]) + ' ' + Y(p[1])).join(''), ...stroke, 'stroke-dasharray': '6 4', style: 'stroke:var(--accent)' })));
    const segs = opts.segments;
    const cur = segs.length ? segs[segs.length - 1] : null;
    if (cur && cur.points.length) {
      const lp = cur.points[cur.points.length - 1];
      svg.append(s('path', { d: `M${X(lp[0])} ${Y(lp[1])}L${X(fan[0][0])} ${Y(fan[0][1])}`, ...stroke, 'stroke-dasharray': '2 3', style: 'stroke:var(--text)' }));
    }
  }
  const plot = h('div.chart', { style: `height:${H}px;margin:12px 0 0 ${left}px` });
  for (const v of [0, 50, 100]) {
    const t = ((1 - v / ymax) * 100).toFixed(2) + '%';
    plot.append(h('div.gridline', { style: `top:${t}` + (v === 100 ? ';border-top:1px dashed var(--accent)' : '') }),
      h('span.ylab', { style: `left:-${left}px;top:${t}`, text: `${v}%` }));
  }
  plot.append(svg);
  // only the readings that hit the limit get a mark; the others are the line itself
  const xp = (t) => ((t - x0) / (x1 - x0) * 100).toFixed(2) + '%';
  const yp = (v) => ((1 - v / ymax) * 100).toFixed(2) + '%';
  for (const p of hover) {
    if (p.hit && p.t >= x0 && p.t <= x1) plot.append(h('span.enddot', { style: `left:${xp(p.t)};top:${yp(p.v)};background:var(--accent-strong)`, title: 'Rate limit hit' }));
  }
  for (const g of gaps) {
    if ((g.b[0] - Math.max(g.a[0], x0)) / (x1 - x0) < 0.12) continue; // too narrow to label
    const mid = (Math.max(g.a[0], x0) + g.b[0]) / 2;
    const v = g.a[1] + (g.b[1] - g.a[1]) * (mid - g.a[0]) / (g.b[0] - g.a[0]);
    plot.append(h('span.gaplab', { style: `left:${xp(mid)};top:${yp(v)}`, text: g.label }));
  }
  if (opts.now && opts.now < x1 && opts.now > x0) {
    const f = ((opts.now - x0) / (x1 - x0) * 100).toFixed(2) + '%';
    plot.append(h('div.nowline', { style: `left:${f}` }), h('span.nowlab', { style: `left:${f}`, text: 'now' }));
  }
  if (runout) {
    const y100 = yp(100), fx = (f) => (f * 100).toFixed(2) + '%';
    // the plot's width is not known until it is laid out: a narrow one (the
    // label spans more of it) is the cautious guess, as the forecast rises
    const lab = runoutLabel(runout.f, fan, { x0, x1, w: PLOT_GUESS, h: H, ymax }, LABEL_BOX,
      hover.map((p) => [p.t, p.v]));
    plot.append(h('div.runguide', { style: `left:${fx(runout.f)};top:${y100}` }),
      h('span.rundot', { style: `left:${fx(runout.f)};top:${y100}`, title: runout.title }),
      h(`div.runlab.${lab.side}${lab.clear ? '' : '.backed'}`, { style: `left:${fx(runout.f)};top:${lab.top.toFixed(1)}px` },
        h('span.v', { text: runout.title }), runout.sub ? h('span.r', { text: runout.sub }) : null));
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

// `account` (optional): the account the card is for, named under its label.
export function limitCard(label, w, lim, account) {
  const head = (pct, big, small, warn) => h('div', { style: 'display:flex;align-items:center;gap:16px' }, ring(pct, warn),
    h('div', { style: 'display:flex;flex-direction:column;gap:2px;min-width:0' }, h('span.label', { text: label }),
      account ? h('span.note.acct', { text: account.label + (account.current ? ' · current' : ''), title: account.label }) : null,
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
    const since = lim.recording_since ? new Date(lim.recording_since).getTime() : null;
    card.append(limitChart({ segments: [{ points: w.history, reset: false, start_known: w.start_known }], fan: fc ? fc.fan : [], now: fc ? fc.fan[0][0] : null, x0, x1, height: 110, weekly, since, runout: runoutMark(fc, x0, x1, weekly) }));
  }
  const fit = fitNote(w);
  if (fit) card.append(h('span.note', { text: fit }));
  if (!lim.recording) card.append(notRecording(lim));
  const q = account && (lim.accounts || []).length > 1 ? `?account=${encodeURIComponent(accountKey(account))}` : '';
  card.append(h('a.note', { href: '/limits' + q, text: 'Every window, and what spent it →' }));
  return card;
}

// ------------------------------------------------------------------ accounts

// An account's key in the URL (`?account=`) and in the remembered choice: its email,
// or its id when there is none.
export const accountKey = (a) => a.email || a.id;
const STORE = 'capcom.limits.account';

// The account the page shows: 'all', or one of `lim.accounts`, from `?account=`, else
// the choice remembered in this browser; a single account is always itself.
export function chosenAccount(lim, query) {
  const list = lim.accounts || [];
  if (list.length < 2) return list[0] || null;
  let key = query.get('account');
  if (key === null) {
    try { key = localStorage.getItem(STORE); } catch { key = null; }
  }
  return list.find((a) => accountKey(a) === key || a.id === key) || 'all';
}

export function accountTabs(lim, chosen) {
  const list = lim.accounts || [];
  if (list.length < 2) return null;
  const cur = chosen === 'all' ? 'all' : accountKey(chosen);
  const el = seg('Account', [['all', 'All accounts'], ...list.map((a) => [accountKey(a), a.label])], cur, (v) => {
    try { localStorage.setItem(STORE, v); } catch { /* private window: not remembered */ }
    setQuery({ account: v === 'all' ? null : v });
  });
  el.classList.add('acct');
  for (const b of el.querySelectorAll('button')) b.title = b.textContent;
  return el;
}

// Dollars of API-equivalent spend left before every account's weekly limit, from
// each account's own fit, and when all of it is spent at the recent pace.
export function combinedCard(lim) {
  const c = lim.combined;
  if (!c) return null;
  const labels = new Map((lim.accounts || []).map((a) => [a.id, a.label]));
  const card = h('section.card', { style: 'grid-column:1/-1' },
    h('div', { style: 'display:flex;flex-direction:column;gap:2px' },
      h('span.label', { text: 'All accounts, before their weekly limits' }),
      h('span.big', { style: c.usd_left === null ? 'color:var(--muted)' : null }, c.usd_left === null ? 'No fit yet' : usd(c.usd_left),
        c.usd_left === null ? null : h('small', { text: ' of API-equivalent spend left' }))));
  const max = Math.max(1e-9, ...c.accounts.map((a) => a.usd_left || 0));
  const rows = h('div.blist');
  for (const a of c.accounts) {
    const pct = a.percent === null ? 'no reading' : `${pctText(a.percent)} used`;
    const per = a.usd_per_percent === null ? 'no fit yet' : `${usd(a.usd_per_percent)} per 1%`;
    const reset = a.resets_at ? ` · resets ${when(a.resets_at, true)}` : '';
    rows.append(h('div.bitem', null,
      h('div.top', null, h('span', { text: a.label }), h('span.mono', { text: a.usd_left === null ? '–' : usd(a.usd_left) })),
      h('div.stackbar', { 'aria-hidden': 'true' }, h('span', { style: `width:${((a.usd_left || 0) / max * 100).toFixed(2)}%;background:var(--accent)` })),
      h('span.note', { text: `${pct} · ${per}${reset}` })));
  }
  card.append(rows);
  let line = null;
  if (c.runs_out_at) {
    line = `At the pace of the last ${c.pace_hours} hours (${usd(c.pace_usd_per_hour)} an hour), all of it is spent around ${when(c.runs_out_at, true)}.`;
    if (c.reset_first) line += ` Before then, ${labels.get(c.reset_first.id) || 'one account'}'s week resets (${when(c.reset_first.resets_at, true)}).`;
  } else if (c.usd_left === 0) {
    line = 'Every account with a fit is at its weekly limit.';
  } else if (c.usd_left !== null) {
    line = `Nothing spent in the last ${c.pace_hours} hours, so no run-out time.`;
  }
  if (line) card.append(h('span.note', { text: line }));
  const missing = c.accounts.filter((a) => a.usd_left === null).map((a) => a.label);
  if (missing.length) card.append(h('span.note', { text: `Not counted: ${missing.join(', ')} (no fit of its weekly limit yet; it needs two weekly readings with spend between them).` }));
  const earlier = c.accounts.some((a) => a.id === 'unknown') ? ' "Earlier account": readings from before capcom recorded accounts, on no known account\'s weekly cycle.' : '';
  card.append(h('span.note', { text: `Each account's dollars left: (100% − its weekly percentage) × its own dollars per 1%.${earlier}` }));
  return card;
}

// The Overview's limit cards: the current account's two (or the chosen one's), or
// with "All accounts" every account's two side by side and the combined card.
export function limitCards(lim, query) {
  const list = lim.accounts || [];
  const chosen = chosenAccount(lim, query);
  const wins = (a) => (a && a.windows) || lim.windows || {};
  const pair = (a, named) => [limitCard('5-hour window', wins(a).five_hour, lim, named ? a : null), limitCard('Weekly limit', wins(a).seven_day, lim, named ? a : null)];
  const out = [];
  const tabs = accountTabs(lim, chosen);
  if (tabs) out.push(h('div.split', null, h('span.label', { text: 'Limits by account' }), tabs));
  const named = list.length > 1 || (list[0] && list[0].email);
  const cards = chosen === 'all' ? [...list.flatMap((a) => pair(a, true)), combinedCard(lim)] : pair(chosen, named);
  out.push(h('div.grid', { style: '--min:440px' }, cards));
  return out;
}

// ------------------------------------------------------------------ page

// Where the page's chart starts: at the start of the oldest window still in the
// range (`days` back from now), not at an arbitrary time before it.
export function chartStart(segs, now, days) {
  const lo = now - days * 864e5;
  let x0 = Infinity;
  for (const sg of segs) {
    const pts = sg.points;
    if (!pts.length) continue;
    const end = sg.resets_at ? new Date(sg.resets_at).getTime() : pts[pts.length - 1][0];
    if (end < lo) continue;
    const start = sg.start ? new Date(sg.start).getTime() : pts[0][0];
    x0 = Math.min(x0, Number.isFinite(start) ? start : pts[0][0]);
  }
  return Number.isFinite(x0) && x0 < now ? x0 : now - days * 864e5;
}

function windowSection(w, now, days, since) {
  const weekly = isWeekly(w);
  const card = h('section.card', null, h('div.split', null, h('h2', { text: w.label[0].toUpperCase() + w.label.slice(1) }),
    h('span.note', { text: `${w.samples} sample${w.samples === 1 ? '' : 's'}` })));
  if (!w.reading) { card.append(empty(null, 'No readings of this window yet.')); return card; }
  card.append(h('p.note', { text: summary(w).join(' ') }));
  const segs = [...w.segments].reverse(); // oldest first
  const fc = w.forecast;
  const x0 = chartStart(segs, now, days);
  const end = fc ? new Date(fc.resets_at).getTime() : now;
  const x1 = Math.max(end, now);
  card.append(limitChart({
    segments: segs,
    fan: fc ? fc.fan : [], now, x0, x1, height: 180, weekly: true, since, runout: runoutMark(fc, x0, x1, weekly),
  }));
  const fit = fitNote(w);
  if (fit) card.append(h('p.note', { text: fit }));
  if (w.fit && w.fit.method === 'per_type' && w.fit.weights.length > 1) {
    card.append(h('h3', { text: 'What 1% costs, by model and token type' }));
    card.append(h('div.blist', null, w.fit.weights.slice(0, 8).map((x) => h('div.bitem', null, h('div.top', null,
      h('span', { text: `${x.model.replace(/^claude-/, '')} ${x.category.replace('_', ' ')}` }),
      h('span.mono', { text: `${tokens(Math.round(1 / x.percent_per_mtok * 1e6))} tokens` }))))));
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
        h('span.note', { text: sg.by_project.map((p) => `${p.project} ${usd(p.usd)}${p.percent === null ? '' : ` (≈${pctText(p.percent, 1)})`}`).join(' · ') }),
        sg.inferred_usd >= 0.005 ? h('span.note', { text: `Of which ${usd(sg.inferred_usd)} from sessions with no reading, put on this account by time (inferred).` }) : null));
    }
    card.append(h('h3', { text: 'What spent each window' }), rows);
  }
  return card;
}

// The range tabs, with the ranges that would show nothing more than a shorter one
// disabled; none at all while the history fits in the shortest range.
export function dayTabs(days, since, now) {
  const span = since ? (now - since) / 864e5 : 0;
  if (span <= DAY_OPTIONS[0][0]) return null;
  const opts = DAY_OPTIONS.map(([v, text], i) => {
    const prev = i ? DAY_OPTIONS[i - 1][0] : 0;
    return [v, text, prev >= span ? `Recording began ${Math.ceil(span)} days ago: ${text} would show nothing more` : null];
  });
  return seg('Window', opts, days, (v) => setQuery({ days: v === 14 ? null : v }));
}

export async function renderLimits(main, r) {
  const days = DAY_OPTIONS.map((o) => o[0]).includes(+r.query.get('days')) ? +r.query.get('days') : 14;
  const d = await api(`limits?days=${days}`);
  const now = Date.now();
  const since = d.recording_since ? new Date(d.recording_since) : null;
  main.append(h('header.head', null,
    h('div.titles', null, h('h1', { text: 'Limits' }),
      h('p.sub', { text: 'The 5-hour and weekly windows of your plan: how full, when they reset, and what filled them.' }),
      since ? h('p.sub', { text: `Recording since ${monthDay(since)}, ${hm(since)}` }) : null),
    dayTabs(days, since ? since.getTime() : null, now)));
  if (!d.available) {
    main.append(h('section.card', null, notRecording(d)));
    return;
  }
  if (!d.recording) main.append(h('section.card', null, notRecording(d)));
  const chosen = chosenAccount(d, r.query);
  const tabs = accountTabs(d, chosen);
  if (tabs) main.append(h('div.split', null, h('span.label', { text: 'Account' }), tabs));
  const sections = (wins) => ['five_hour', 'seven_day'].filter((k) => wins[k]).map((k) => windowSection(wins[k], now, days, since ? since.getTime() : null));
  if (chosen === 'all') {
    const comb = combinedCard(d);
    if (comb) main.append(h('div.grid', null, comb));
    for (const a of d.accounts) {
      main.append(h('div.split', null, h('h2.small.acct', { text: a.label }), h('span.note', { text: a.current ? 'current account' : `${a.samples} samples` })),
        h('div.grid', { style: '--min:520px' }, sections(a.windows)));
    }
  } else {
    if (chosen && chosen.email && (d.accounts || []).length > 1) main.append(h('h2.small.acct', { text: chosen.label }));
    for (const el of sections((chosen && chosen.windows) || d.windows)) main.append(el);
  }
  main.append(h('p.note', { text: 'Percentages come from Claude Code\'s status line. Usage from other machines or claude.ai counts toward the limits but is not in these logs, so the fit treats it as error.' }));
}
