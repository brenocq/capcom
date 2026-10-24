// Limits: the subscription's 5-hour and weekly windows. The Overview's two cards
// (the reading, the forecast fan) and the Limits page (each window's sawtooth over
// time, and what spent each window by project). Data from /api/limits.

import {
  h, s, api, usd, tokens, seg, empty, setQuery, hm, monthDay, dayName, showTip, hideTip, entityColors, segBar, tableToggle,
  GROUPS, pickedGroup, rememberGroup, limitsPath, labelSpan, RANGES, rangeTabs, LIMIT_DAYS,
} from './lib.js';
import { memberBands, timeTicks, xAxis } from './charts.js';
import { TWEEN_MS } from './loading.js';
import { withColors } from './spend.js';

let uid = 0;

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

// Where a chart draws each window's boundary: every window's start and reset in
// [x0, x1], each once, as {t, f (0 to 1 across the plot), kind, exact, label (the
// time), title, prio}. kind: 'reset' (a reset, which also starts the next window
// when one began then), 'start' (a window that began after an idle gap, not at the
// reset before it) or 'next' (the open window's coming reset). `exact`: from a
// recorded reset time (Claude Code's `resets_at`: a reset, and a start as the reset
// minus the window's length); a start capcom only saw as the first reading of a
// window (no reset time known, or a drop) is not exact, and its label says "~".
// `prio` orders the labels when they don't all fit: the coming reset, then the open
// window's start, then the rest newest first. segments: oldest first, as limitChart
// takes them; weekly: the labels name the day; now (ms): what is still to come.
const SAME = 120e3; // a start this close to a reset is the same boundary
export function windowBounds(segments, x0, x1, weekly, now = Date.now()) {
  const at = (t) => (weekly ? `${monthDay(new Date(t))} ${hm(new Date(t))}` : hm(new Date(t)));
  const out = [];
  const add = (t, kind, exact, open) => {
    if (!(t >= x0 && t <= x1)) return;
    const same = out.find((b) => Math.abs(b.t - t) <= SAME);
    if (same) {
      // a reset that starts the next window: one line, one label
      if (kind === 'start' && same.kind !== 'start') { same.starts = true; same.open = same.open || open; }
      return;
    }
    out.push({ t, kind, exact, open });
  };
  segments.forEach((sg) => {
    if (!sg.resets_at) return;
    const t = new Date(sg.resets_at).getTime();
    add(t, t > now ? 'next' : 'reset', true, false);
  });
  segments.forEach((sg, i) => {
    const open = i === segments.length - 1 && !!sg.resets_at && new Date(sg.resets_at).getTime() > now;
    const start = sg.start ? new Date(sg.start).getTime() : sg.start_known && sg.points && sg.points.length ? sg.points[0][0] : null;
    if (start !== null) add(start, 'start', !!sg.start_known, open);
  });
  out.sort((p, q) => p.t - q.t);
  const span = x1 - x0 || 1;
  for (const b of out) {
    const time = at(b.t);
    b.f = (b.t - x0) / span;
    b.label = (b.exact ? '' : '~') + time;
    b.name = b.kind === 'next' ? 'Resets' : b.kind === 'reset' ? 'Window reset' : b.exact ? 'Window started' : 'Window started by';
    b.title = `${b.name} ${time}${b.kind === 'reset' && b.starts ? '; the next window started then' : ''}${b.exact ? '' : ' (its first reading; the reset time was not recorded)'}`;
    b.prio = b.kind === 'next' ? 2 : b.open ? 1 : 0;
  }
  // newest first within a priority
  const order = [...out].sort((p, q) => q.prio - p.prio || q.t - p.t);
  order.forEach((b, i) => { b.rank = i; });
  return out;
}

// Which row each boundary label takes above the plot, once its width is known:
// labels in rank order (`rank`, lowest first) take the lowest row where they touch
// no label already there (nor `avoid`: [a, b] px spans on row 0, the "now" label),
// and where no line crosses them: a label's line reaches up to its row, through the
// rows under it, so a label never sits on another one's line nor its own line on a
// label below. In a row a label tries three places: centered on its line, then
// beside it as a flag (right, then left; its line then runs up along it), kept inside
// the plot's width; first those that cover the fewest other boundaries' lines (each
// counted by its `weight`, 1 unless given, so a label steps aside from the line of the
// coming reset). items: [{x (px), w (px), rank, weight}]; returns [{row (-1: no room),
// left (px), flag}] in items' order. Pure.
export const LABEL_ROWS = 3;
export function placeLabels(items, width, avoid = [], rows = LABEL_ROWS, pad = 6) {
  const taken = Array.from({ length: rows }, (_, r) => (r === 0 ? avoid.map(([a, b]) => [a, b]) : []));
  const lines = []; // [x, row of its label]
  const out = items.map(() => ({ row: -1, left: 0, flag: false }));
  const order = items.map((it, i) => i).sort((i, j) => items[i].rank - items[j].rank);
  const near = 3;
  for (const i of order) {
    const it = items[i];
    const clamp = (l) => Math.max(0, Math.min(width - it.w, l));
    const all = [[clamp(it.x - it.w / 2), false], [clamp(it.x + near), true], [clamp(it.x - near - it.w), true]];
    // first the places that leave the most other boundaries' lines free to reach up
    const covers = ([a]) => items.reduce((n, o) => n + (o !== it && o.x > a - near && o.x < a + it.w + near ? (o.weight ?? 1) : 0), 0);
    const spots = all.map((x) => [x, covers(x)]).sort((p, q) => p[1] - q[1]).map((x) => x[0]);
    out[i].left = spots[0][0];
    rowLoop: for (let r = 0; r < rows; r++) {
      // its own line passes the rows under r: no label there may cover it
      let crosses = false;
      for (let q = 0; q < r && !crosses; q++) crosses = taken[q].some(([p, e]) => it.x > p - near && it.x < e + near);
      if (crosses) break; // nor in any row higher up
      for (const [a, flag] of spots) {
        const b = a + it.w;
        if (taken[r].some(([p, q]) => a < q + pad && p < b + pad)) continue;
        if (lines.some(([x, row]) => row > r && x > a - near && x < b + near)) continue;
        taken[r].push([a, b]);
        lines.push([it.x, r]);
        out[i] = { row: r, left: a, flag };
        break rowLoop;
      }
    }
  }
  return out;
}

// Usage windows over time: each window's readings as a rising line that drops to
// 0 at its reset, a dashed line at 100%, and the current window's forecast as a
// fan (the 80% range, filled, with hairline edges, and the median dashed).
// Readings are drawn as the line alone (dense dots read as noise); only a reading
// that hit the limit gets a mark. The stretch from a window's reset (0 %) to its first
// reading is dashed and labeled, since nothing was read there.
// opts: {segments: [{points, resets_at, reset, hit_limit, start_known}], fan, now, x0, x1,
//        height, left, weekly, since (ms: when recording began), runout (runoutMark),
//        fitted: [[ms, percent]], bands: [{label, color, values}] (each member's own percent
//        at fitted's times, largest first)}
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
  const defs = s('defs', null, s('clipPath', { id: id + 'c' }, s('rect', { x: 0, y: 0, width: W, height: H })));
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true', style: 'overflow:hidden' }, defs);
  const hover = [];
  const stroke = { fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' };
  const gaps = [];
  // the fitted line split into its members (the window page), drawn as Spend draws
  // its members: each one's own line from zero, largest first, a faded band down
  // to the next one, and a fainter band from the fitted line down to the largest;
  // drawn first, under the readings and the fitted line
  const fitted = opts.fitted || [];
  const bands = fitted.length > 1 ? opts.bands || [] : [];
  if (bands.length) {
    const marks = memberBands({
      id, xs: fitted.map((p) => X(p[0])), Y, lines: bands.map((b) => b.values), colors: bands.map((b) => b.color),
      total: fitted.map((p) => p[1]), tint: 'var(--text)', cls: 'fitband',
    });
    defs.append(...marks.defs);
    svg.append(...marks.fills, ...marks.strokes);
  }
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
    if (sg.reset && sg.resets_at) d += 'L' + X(new Date(sg.resets_at).getTime()) + ' ' + Y(last[1]);
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
  // the fitted percent of what was spent, adding up (the window page), on top of its
  // members as Spend's total is: dashed, in the accent, so it reads apart from them
  if (fitted.length > 1) {
    svg.append(s('path', { d: fitted.map((p, i) => (i ? 'L' : 'M') + X(p[0]) + ' ' + Y(p[1])).join(''), ...stroke, 'stroke-dasharray': '5 4', style: 'stroke:var(--accent)' }));
  }
  const bounds = opts.bounds === false ? []
    : windowBounds(opts.segments, x0, x1, opts.weekly && opts.kind !== 'five_hour', opts.now || Date.now());
  // before layout, the label rows from a guessed plot width and label widths
  const guess = placeLabels(bounds.map((b) => ({ x: b.f * PLOT_GUESS, w: b.label.length * 6.4 + 2, rank: b.rank })), PLOT_GUESS);
  const rows = Math.max(0, ...guess.map((g) => g.row + 1));
  const plot = h('div.chart', { style: `height:${H}px;margin:${ROW_TOP[rows]}px 0 0 ${left}px` });
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
  const nowF = opts.now && opts.now < x1 && opts.now > x0 ? (opts.now - x0) / (x1 - x0) : null;
  // each boundary: a thin line (solid when its time was recorded, dashed when only
  // seen as a window's first reading) and its time above the plot, in the row
  // placeLabels gives it; the coming reset and the open window's start first
  bounds.forEach((b, i) => {
    const at = (b.f * 100).toFixed(2) + '%';
    const row = guess[i].row;
    const shift = b.f < 0.03 ? '0' : b.f > 0.97 ? '-100%' : '-50%';
    plot.append(h('div.winbound', {
      'data-i': i, title: b.title,
      style: `position:absolute;top:${row < 0 ? 0 : guess[i].flag ? LAB_TOP[row] + 2 : LINE_TOP[row]}px;bottom:0;left:${at};border-left:1.5px ${b.exact ? 'solid' : 'dashed'} var(--muted);opacity:0.55;pointer-events:none`,
    }));
    plot.append(h('span.winlab', {
      'data-i': i, 'data-f': b.f, title: b.title, text: b.label,
      style: `position:absolute;top:${LAB_TOP[Math.max(row, 0)]}px;left:${at};transform:translateX(${shift});font-size:11px;line-height:14px;color:var(--muted);white-space:nowrap;pointer-events:none${row < 0 ? ';visibility:hidden' : ''}`,
    }));
  });
  if (nowF !== null) {
    const f = (nowF * 100).toFixed(2) + '%';
    plot.append(h('div.nowline', { style: `left:${f}` }), h('span.nowlab', { 'data-f': nowF, style: `left:${f};top:${LAB_TOP[0]}px;line-height:14px`, text: 'now' }));
  }
  const unlabeled = h('p.note.boundnote', { style: 'margin:2px 0 0', hidden: true });
  if (bounds.length) fitLabels(plot, bounds, unlabeled);
  if (runout) {
    const y100 = yp(100), fx = (f) => (f * 100).toFixed(2) + '%';
    // the plot's width is not known until it is laid out: a narrow one (the
    // label spans more of it) is the cautious guess, as the forecast rises
    const reads = hover.map((p) => [p.t, p.v]);
    const place = (w, box) => runoutLabel(runout.f, fan, { x0, x1, w, h: H, ymax }, box, reads);
    const lab = place(PLOT_GUESS, LABEL_BOX);
    const runlab = h(`div.runlab.${lab.side}${lab.clear ? '' : '.backed'}`, { style: `left:${fx(runout.f)};top:${lab.top.toFixed(1)}px` },
      h('span.v', { text: runout.title }), runout.sub ? h('span.r', { text: runout.sub }) : null);
    plot.append(h('div.runguide', { style: `left:${fx(runout.f)};top:${y100}` }),
      h('span.rundot', { style: `left:${fx(runout.f)};top:${y100}`, title: runout.title }), runlab);
    fitRunout(plot, runlab, runout.f, place);
  }
  const label = (t) => `${opts.weekly ? monthDay(new Date(t)) + ' ' : ''}${hm(new Date(t))}`;
  plot.addEventListener('mousemove', (e) => {
    const r = plot.getBoundingClientRect();
    const t = x0 + (e.clientX - r.left) / r.width * (x1 - x0);
    const rows = [];
    let best = null;
    for (const p of hover) if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    if (best && Math.abs(best.t - t) < (x1 - x0) / 40) rows.push({ label: `Reading at ${label(best.t)}`, value: pctText(best.v), color: 'var(--text)' });
    let edge = null;
    for (const b of bounds) if (!edge || Math.abs(b.t - t) < Math.abs(edge.t - t)) edge = b;
    if (edge && Math.abs(edge.t - t) < (x1 - x0) / 60) rows.push({ label: edge.name, value: edge.label.replace(/^~/, ''), color: 'var(--muted)' });
    if (fitted.length > 1 && t >= fitted[0][0]) {
      let j = 0;
      fitted.forEach((p, i) => { if (p[0] <= t) j = i; });
      rows.push({ label: 'Fitted from the spend', value: '≈' + pctText(fitted[j][1], 1), color: 'var(--accent)' });
      for (const b of bands) rows.push({ label: b.label, value: '≈' + pctText(b.values[j], 1), color: b.color });
    }
    if (fan.length > 1 && t >= fan[0][0]) {
      let q = fan[0];
      for (const p of fan) if (p[0] <= t) q = p;
      rows.push({ label: `Forecast by ${label(q[0])}`, value: `${pctText(q[1])} (${pctText(q[2])}–${pctText(q[3])})`, color: 'var(--accent)' });
    }
    if (rows.length) showTip(e, label(t), rows); else hideTip();
  });
  plot.addEventListener('mouseleave', hideTip);
  return h('div', null, plot, xAxis(timeTicks(x0, x1, 6), x0, x1, left), unlabeled);
}

// The label rows, px from the plot's top: a label's top, its line's top (the line
// reaches up to its label) and the plot's top margin for that many rows.
const LAB_TOP = [-18, -32, -46], LINE_TOP = [-4, -18, -32], ROW_TOP = [12, 20, 34, 48];

// Once laid out (and on every resize), place the boundary labels by their measured
// widths (placeLabels): each in the lowest row with room. The "now" label goes
// first, stepping aside from the coming reset's line so that one still finds room a
// row up. A boundary label
// with no room left is hidden, and `note` says how many and where to find them (on
// hover), so none goes missing silently.
function fitLabels(plot, bounds, note) {
  const fit = () => {
    const W = plot.getBoundingClientRect().width;
    if (!W) return;
    const labs = [...plot.querySelectorAll('span.winlab')];
    const lines = [...plot.querySelectorAll('div.winbound')];
    const nowLab = plot.querySelector('span.nowlab'), nowLine = plot.querySelector('div.nowline');
    const items = labs.map((el, i) => ({ x: bounds[i].f * W, w: el.getBoundingClientRect().width, rank: bounds[i].rank, weight: bounds[i].kind === 'next' ? 5 : 1 }));
    if (nowLab) {
      items.push({ x: Number(nowLab.dataset.f) * W, w: nowLab.getBoundingClientRect().width, rank: -1 });
      labs.push(nowLab);
      lines.push(nowLine);
    }
    const placed = placeLabels(items, W);
    let used = 0, hidden = 0;
    placed.forEach((pl, i) => {
      const el = labs[i];
      el.style.left = `${pl.left.toFixed(1)}px`;
      el.style.transform = 'none';
      el.style.visibility = pl.row < 0 ? 'hidden' : '';
      el.style.top = `${LAB_TOP[Math.max(pl.row, 0)]}px`;
      if (lines[i]) lines[i].style.top = `${pl.row < 0 ? 0 : pl.flag ? LAB_TOP[pl.row] + 2 : LINE_TOP[pl.row]}px`;
      used = Math.max(used, pl.row + 1);
      if (pl.row < 0 && i < bounds.length) hidden++;
    });
    plot.style.marginTop = `${ROW_TOP[used]}px`;
    note.hidden = !hidden;
    note.textContent = hidden ? `${hidden} of ${bounds.length} window start and reset times have no room for a label at this width; hover the chart at a line for its time.` : '';
  };
  afterLayout(plot, fit);
}

// Run `fit` once the plot is laid out, again on every resize, and again once a page
// swapped in over an older one has tweened its inline styles to the drawn ones
// (morph.js), over the placement made here.
function afterLayout(plot, fit) {
  if (typeof requestAnimationFrame !== 'function') return;
  requestAnimationFrame(fit);
  setTimeout(fit, TWEEN_MS + 80);
  if (typeof ResizeObserver === 'function') new ResizeObserver(fit).observe(plot);
}

// Where the run-out label goes across a plot `width` px wide, once its width `w` is
// measured: beside the guide at `x` on its `side` if it fits, else on the other side,
// else pushed inside the plot's edge (`moved`: it then may cover the forecast, so it
// gets a backing). Pure; returns {left (px), side, moved}.
export function runoutLeft(x, w, width, side, gap = LABEL_GAP) {
  const at = { right: x + gap, left: x - gap - w };
  const fits = (sd) => at[sd] >= 0 && at[sd] + w <= width;
  if (fits(side)) return { left: at[side], side, moved: false };
  const other = side === 'right' ? 'left' : 'right';
  if (fits(other)) return { left: at[other], side: other, moved: false };
  return { left: Math.max(0, Math.min(width - w, at[side])), side, moved: true };
}

// Place the run-out label again at the plot's laid-out width and the label's measured
// size (`place(width, box)`: runoutLabel there), kept inside the plot (runoutLeft); a
// label wider than the plot wraps.
function fitRunout(plot, el, f, place) {
  const fit = () => {
    const W = plot.getBoundingClientRect().width;
    if (!W) return;
    el.style.maxWidth = `${W}px`;
    el.style.whiteSpace = el.scrollWidth > W ? 'normal' : '';
    const box = { w: el.offsetWidth, h: el.offsetHeight };
    const lab = place(W, box);
    const pl = runoutLeft(f * W, box.w, W, lab.side);
    el.style.left = `${pl.left.toFixed(1)}px`;
    el.style.top = `${lab.top.toFixed(1)}px`;
    el.style.transform = 'none';
    el.classList.toggle('left', pl.side === 'left');
    el.classList.toggle('right', pl.side === 'right');
    el.classList.toggle('backed', !lab.clear || pl.moved || pl.side !== lab.side);
  };
  afterLayout(plot, fit);
}

// ------------------------------------------------------------------ overview cards

// `account` (optional): the account the card is for, named under its label.
export function limitCard(label, w, lim, account) {
  const head = (pct, big, small, warn) => h('div', { style: 'display:flex;align-items:center;gap:16px' }, ring(pct, warn),
    h('div', { style: 'display:flex;flex-direction:column;gap:2px;min-width:0' }, h('span.label', { text: label }),
      account ? h('span.note.acct', { text: account.label + (account.current ? ' · current' : ''), title: account.label }) : null,
      h('span.big', { style: big ? null : 'color:var(--muted)' }, big || 'No data yet', small ? h('small', { text: ' ' + small }) : null)));
  if (!lim || !lim.available || !w || !w.reading) {
    const none = lim && lim.recording ? empty(null, account ? 'No reading of this window for this account yet.' : 'No reading of this window yet.') : notRecording(lim || {});
    return h('section.card', null, head(0, null, null, false), none);
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
    card.append(limitChart({ segments: [{ points: w.history, reset: false, start_known: w.start_known }], fan: fc ? fc.fan : [], now: fc ? fc.fan[0][0] : null, x0, x1, height: 110, weekly, since, runout: runoutMark(fc, x0, x1, weekly), bounds: false }));
  }
  const fit = fitNote(w);
  if (fit) card.append(h('span.note', { text: fit }));
  if (!lim.recording) card.append(notRecording(lim));
  card.append(h('a.note', { href: '/limits', text: 'Every window, and what spent it →' }));
  return card;
}

// ------------------------------------------------------------------ accounts

// An account's key in the remembered choice: its email, or its id when there is none.
export const accountKey = (a) => a.email || a.id;
const STORE = 'capcom.limits.account';

// The account Overview and Limits show: 'all', or one of `lim.accounts`, as last
// picked in this browser (never in the URL: it holds an email); 'all' when nothing
// is remembered, storage is off or the account is gone. A single account is always
// itself.
export function chosenAccount(lim) {
  const list = lim.accounts || [];
  if (list.length < 2) return list[0] || null;
  let key = null;
  try { key = localStorage.getItem(STORE); } catch (e) { /* storage off */ }
  return list.find((a) => accountKey(a) === key || a.id === key) || 'all';
}

// Draw the page again for a new choice: the same URL, so the router redraws from
// the replies it has (app.js renders on `capcom-route`).
const redraw = () => window.dispatchEvent(new Event('capcom-route'));
// a choice made in another tab shows here too
if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener('storage', (e) => { if (e.key === STORE) redraw(); });
}

export function accountTabs(lim, chosen) {
  const list = lim.accounts || [];
  if (list.length < 2) return null;
  const cur = chosen === 'all' ? 'all' : accountKey(chosen);
  const el = seg('Account', [['all', 'All accounts'], ...list.map((a) => [accountKey(a), a.label])], cur, (v) => {
    try { localStorage.setItem(STORE, v); } catch (e) { /* storage off: not remembered */ }
    redraw();
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
export function limitCards(lim) {
  const list = lim.accounts || [];
  const chosen = chosenAccount(lim);
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
// range (that ends after `lo`, ms: the range's start), not at an arbitrary time
// before it. `keep`: with no window in the range, the newest one (the weekly chart
// on a short range shows at least its current week).
export function chartStart(segs, now, lo, keep = false) {
  let x0 = Infinity;
  const startOf = (sg) => {
    const pts = sg.points;
    const start = sg.start ? new Date(sg.start).getTime() : pts[0][0];
    return Number.isFinite(start) ? start : pts[0][0];
  };
  for (const sg of segs) {
    const pts = sg.points;
    if (!pts.length) continue;
    const end = sg.resets_at ? new Date(sg.resets_at).getTime() : pts[pts.length - 1][0];
    if (end < lo) continue;
    x0 = Math.min(x0, startOf(sg));
  }
  if (!Number.isFinite(x0) && keep) {
    const last = [...segs].reverse().find((sg) => sg.points.length);
    if (last) x0 = startOf(last);
  }
  return Number.isFinite(x0) && x0 < now ? x0 : lo;
}

// Where the page's chart ends: now, or later at the open window's coming reset
// (and the forecast's), so the forecast reads against the reset.
export function chartEnd(segs, fc, now) {
  let x1 = now;
  const open = segs.length ? segs[segs.length - 1] : null;
  if (open && open.resets_at) x1 = Math.max(x1, new Date(open.resets_at).getTime());
  if (fc && fc.resets_at) x1 = Math.max(x1, new Date(fc.resets_at).getTime());
  return x1;
}

// "What 1% costs": how many tokens of each kind make 1% of the window and their
// dollars, from the fit's own uncertainty (capcom/limits/view.py costs_json): each
// kind with its 80% range ("rough" when it spans more than 2×), the small kinds
// together in dollars, and the kinds the readings cannot tell from zero named apart.
const TOKEN_WORDS = { input: 'input tokens', cache_write: 'cache-write tokens', cache_read: 'cache-read tokens', output: 'output tokens' };
// The merged kinds in few words: "all fable-5 and haiku-5-5 tokens, and opus-5-5
// input" (labels are "<model> <kind>", as costs_json writes them).
export function kindsText(kinds) {
  const by = new Map();
  for (const k of kinds) {
    const i = k.indexOf(' ');
    const m = i < 0 ? k : k.slice(0, i);
    if (!by.has(m)) by.set(m, []);
    by.get(m).push(i < 0 ? '' : k.slice(i + 1));
  }
  const and = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`);
  const whole = [...by].filter(([, ks]) => ks.length >= 4).map(([m]) => m);
  const parts = [...by].filter(([, ks]) => ks.length < 4).map(([m, ks]) => `${m} ${and(ks)}`);
  return and([...(whole.length ? [`all ${and(whole)} tokens`] : []), ...parts]);
}
const money = (x) => (x === null || x === undefined ? '–' : usd(x, x >= 100 ? 0 : 2));
export function costsBlock(w) {
  const c = w.fit && w.fit.costs;
  if (!c || (!c.rows.length && !c.merged)) return null;
  const model = (m) => m.replace(/^claude-/, '');
  const out = [h('h3', { text: 'What 1% costs, by model and token type' })];
  const lead = [`How many tokens of each kind use up 1% of your ${w.label}, and what they would cost at API prices.`];
  const byTok = [...c.rows].sort((a, b) => b.tokens - a.tokens);
  if (byTok.length > 1) {
    const lo = byTok[0], hi = byTok[byTok.length - 1];
    const name = (r) => `${model(r.model)} ${TOKEN_WORDS[r.category] || r.category}`;
    const cap = (t) => t[0].toUpperCase() + t.slice(1);
    lead.push(`${cap(name(lo))} count the least: ${tokens(lo.tokens)} of them make 1%. ${cap(name(hi))} count the most: ${tokens(hi.tokens)} make 1%.`);
  }
  out.push(h('p.note', { style: 'margin:0 0 8px', text: lead.join(' ') }));
  const list = h('div.blist');
  for (const r of byTok) {
    const range = `${tokens(r.tokens_low)} and ${tokens(r.tokens_high)}`;
    list.append(h('div.bitem', null,
      h('div.top', null, h('span', { text: r.label }),
        h('span.mono', { text: `${tokens(r.tokens)} tokens · ${money(r.usd)}` })),
      h('span.note', { text: r.rough ? `Rough: between ${range} tokens (${money(r.usd_low)} to ${money(r.usd_high)}).` : `Likely between ${range} tokens.` })));
  }
  const mg = c.merged;
  if (mg && mg.usd !== null) {
    const share = mg.share === null ? '' : `, ${pctText(mg.share * 100, mg.share < 0.1 ? 1 : 0)} of the spend`;
    list.append(h('div.bitem', null,
      h('div.top', null, h('span', { text: `Everything else (${mg.kinds.length} kind${mg.kinds.length === 1 ? '' : 's'}${share})` }), h('span.mono', { text: money(mg.usd) })),
      h('span.note', { text: `${mg.rough ? `Rough: between ${money(mg.usd_low)} and ${money(mg.usd_high)}. ` : ''}That is ${kindsText(mg.kinds)}.`, title: mg.kinds.join(', ') })));
  }
  out.push(list);
  const rule = [`A kind with less than ${pctText(c.min_share * 100)} of the spend has too few tokens to measure alone, so those are counted together, by their dollars.`];
  if (mg && mg.usd === null) rule.push(`Even together (${kindsText(mg.kinds)}), the readings cannot tell their cost from zero.`);
  if (c.left_out.length) rule.push(`Left out, as the readings cannot tell their cost from zero: ${c.left_out.join(', ')}.`);
  rule.push('The ranges are 80% ranges.');
  out.push(h('p.note', { style: 'margin:8px 0 0', text: rule.join(' ') }));
  return out;
}

function windowSection(w, now, lo, since, group) {
  const weekly = isWeekly(w);
  const card = h('section.card', null, h('div.split', null, h('h2', { text: w.label[0].toUpperCase() + w.label.slice(1) }),
    h('span.note', { text: `${w.samples} sample${w.samples === 1 ? '' : 's'}` })));
  if (!w.reading) { card.append(empty(null, 'No readings of this window yet.')); return card; }
  card.append(h('p.note', { text: summary(w).join(' ') }));
  const segs = [...w.segments].reverse(); // oldest first
  const fc = w.forecast;
  const x0 = chartStart(segs, now, lo, weekly);
  const x1 = chartEnd(segs, fc, now);
  card.append(limitChart({
    segments: segs,
    fan: fc ? fc.fan : [], now, x0, x1, height: 180, weekly: true, kind: w.window, since, runout: runoutMark(fc, x0, x1, weekly),
  }));
  const inferred = windowBounds(segs, x0, x1, weekly, now).some((b) => !b.exact);
  card.append(h('p.note', { text: `The thin lines mark each window's start and reset, at the times Claude Code recorded${inferred ? '; a dashed one, marked ~, is only where a window\'s first reading was, since its reset time was not recorded' : ''}.` }));
  const fit = fitNote(w);
  if (fit) card.append(h('p.note', { text: fit }));
  const costs = costsBlock(w);
  if (costs) card.append(...costs);
  // what spent each window, newest first: one bar per window split by the page's
  // group, a color per member (the same member keeps its color in every window);
  // each part's dollars and share of the limit on hover, all of them in the table
  const spent = w.segments.filter((sg) => sg.by && sg.by.length).slice(0, weekly ? 3 : 6);
  if (spent.length) {
    const keys = [...new Set(spent.flatMap((sg) => sg.by.map((p) => p.key)))];
    const order = keys.filter((k) => k !== '(other)').concat(keys.includes('(other)') ? ['(other)'] : []);
    const members = new Map(withColors(order.map((key) => ({ key, label: key })), group).map((m) => [m.key, m]));
    const rows = h('div.blist');
    const table = [];
    for (const sg of spent) {
      const from = sg.start ? `${monthDay(new Date(sg.start))} ${hm(new Date(sg.start))}` : '';
      const to = sg.reset && sg.resets_at ? hm(new Date(sg.resets_at)) : 'now';
      const span = `${from} → ${to}`;
      const total = sg.usd || 1e-9;
      const parts = sg.by.map((p) => ({
        color: members.get(p.key).color, share: p.usd / total, title: members.get(p.key).label,
        rows: [{ label: 'Spent', value: usd(p.usd) }, { label: 'Of the window', value: pctText(p.usd / total * 100, 1) },
          p.percent === null ? null : { label: 'Of the limit', value: '≈' + pctText(p.percent, 1) }].filter(Boolean),
      }));
      // the row opens the window on its own page
      const text = `${span} · peak ${pctText(sg.peak)}${sg.hit_limit ? ' · limit hit' : ''}`;
      rows.append(h(sg.path ? 'a.bitem.winrow' : 'div.bitem', sg.path ? { href: sg.path, style: 'display:block;color:inherit;text-decoration:none', title: 'Open this window' } : null,
        h('div.top', null, h('span', null, text, sg.path ? h('span', { style: 'color:var(--clay)', text: ' →' }) : null), h('span.mono', { text: usd(sg.usd) })),
        segBar(parts, { aria: `${span}: ${usd(sg.usd)}`, focus: !sg.path }),
        sg.inferred_usd >= 0.005 ? h('span.note', { text: `Of which ${usd(sg.inferred_usd)} from sessions with no reading, put on this account by time (inferred).` }) : null));
      for (const p of sg.by) table.push([span, members.get(p.key).label, usd(p.usd), p.percent === null ? '' : '≈' + pctText(p.percent, 1)]);
    }
    const groupName = GROUPS.find(([v]) => v === group)[1];
    card.append(h('div.split', null, h('h3', { text: 'What spent each window' }),
      seg('Split by', GROUPS, group, (v) => { rememberGroup(v); setQuery({ group: v }); })),
    h('div.legend', { style: 'margin:0 0 10px' }, order.map((k) => h('span', null, h('i', { style: `background:${members.get(k).color}` }), labelSpan(members.get(k).label)))),
    rows, tableToggle(['Window', groupName, 'Spent', 'Of the limit'], table));
  }
  return card;
}

// The range picker (Spend's), with the ranges that would show nothing more than a
// shorter one disabled.
export function dayTabs(days, since, now) {
  const span = since ? (now - since) / 864e5 : Infinity;
  const list = RANGES.map((r) => r[0]);
  return rangeTabs(days, (v) => {
    const i = list.indexOf(v);
    const prev = i > 0 ? list[i - 1] : 0;
    return prev > 1 && prev >= span ? `Recording began ${Math.ceil(span)} days ago: ${RANGES[i][1]} would show nothing more` : null;
  });
}

export async function renderLimits(main, r) {
  const days = LIMIT_DAYS.includes(+r.query.get('days')) ? +r.query.get('days') : 14;
  const group = pickedGroup(r.query);
  const d = await api(limitsPath(r.query));
  const now = Date.now();
  const since = d.recording_since ? new Date(d.recording_since) : null;
  main.append(h('header.head', null,
    h('div.titles', null, h('h1', { text: 'Limits' }),
      h('p.sub', { text: 'The 5-hour and weekly windows of your plan: how full, when they reset, and what filled them.' }),
      since ? h('p.sub', { text: `Recording since ${monthDay(since)}, ${hm(since)}` }) : null),
    h('div.controls', null, dayTabs(days, since ? since.getTime() : null, now))));
  if (!d.available) {
    main.append(h('section.card', null, notRecording(d)));
    return;
  }
  if (!d.recording) main.append(h('section.card', null, notRecording(d)));
  const chosen = chosenAccount(d);
  const tabs = accountTabs(d, chosen);
  if (tabs) main.append(h('div.split', null, h('span.label', { text: 'Account' }), tabs));
  // the range's start: local midnight `days` - 1 days back, as Spend's
  const lo = d.since ? new Date(d.since).getTime() : now - days * 864e5;
  const sections = (wins) => ['five_hour', 'seven_day'].filter((k) => wins[k]).map((k) => windowSection(wins[k], now, lo, since ? since.getTime() : null, group));
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
