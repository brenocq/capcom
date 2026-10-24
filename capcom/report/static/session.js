// Session view: cost and its check against Claude Code's own estimate, the
// per-request context and cost on an active-time axis, pauses, compactions, the
// main thread and the subagents as lanes, and the agents table.

import {
  h, s, api, usd, tokens, pct, duration, stamp, hm, monthDay, empty, tipOn, showTip, hideTip,
  outputUsd, lowerMark, LOWER_TITLE, CATEGORY_LABELS, CATEGORY_ORDER, categoryColor, entityColor, maxOf,
  spikeText, spikeHref, sessionDot,
} from './lib.js';
import { niceTicks, thinned } from './charts.js';
import { timelineMissing } from './spend.js';

// agent types whose lanes are open, kept across redraws (a theme switch, another session)
const expandedTypes = new Set();

export async function renderSession(main, r) {
  const d = await api('session/' + encodeURIComponent(r.arg || ''));
  // the API's times are ISO; the charts below place them in epoch ms. The reply is
  // kept for this view's redraws (a theme switch, a newer partial reply), so a time
  // already turned into ms is left as it is.
  const ms = (v) => (typeof v === 'string' ? Date.parse(v) : v);
  for (const p of d.pauses) { p.start = ms(p.start); p.end = ms(p.end); }
  for (const c of d.compactions) c.t = ms(c.t);
  for (const a of [d.main, ...d.agents]) if (a) { a.first = a.first && ms(a.first); a.last = a.last && ms(a.last); }
  const sn = d.session, t = d.totals;
  const first = sn.first_seen ? new Date(sn.first_seen) : null, last = sn.last_seen ? new Date(sn.last_seen) : null;

  const chips = [
    h('span.chip', { style: 'display:flex;align-items:center;gap:6px' }, h('span', { class: sn.running ? 'dot hot' : 'dot', style: sn.running ? null : 'background:var(--gray)' }), sn.running ? 'running' : 'ended'),
    sn.cwd ? h('span.chip.mono', { text: sn.cwd }) : null,
    sn.git_branch ? h('span.chip', { text: sn.git_branch }) : null,
    modelChip(sn),
    first ? h('span.chip', { text: `${monthDay(first)} ${hm(first)} to ${sn.running ? 'now' : stamp(last)} · ${duration(d.active_seconds)} active` }) : null,
    h('span.chip', { text: `${d.agents.length} subagent${d.agents.length === 1 ? '' : 's'} · ${d.compactions.length} compaction${d.compactions.length === 1 ? '' : 's'}` }),
  ];
  main.append(h('header', { style: 'display:flex;flex-direction:column;gap:10px' },
    h('nav.crumb', { 'aria-label': 'Breadcrumb' }, h('a', { href: '/sessions', text: 'Sessions' }), h('span', { 'aria-hidden': 'true', text: '/' }), h('span', { text: sn.project, title: sn.project_path || null })),
    h('div.head', null,
      h('div.titles', { style: 'gap:8px' }, h(sn.color ? 'h1.sname' : 'h1', null, sessionDot(sn.color), h('span', null, sn.title || 'Untitled session ' + sn.session_id.slice(0, 8))),
        sn.title ? h('p.note', { style: 'margin:0', text: sn.title_custom ? 'Your name for it (/rename)' : 'Title by Claude Code',
          title: (sn.title_custom ? 'The name you gave the session in Claude Code (/rename)' : 'The title Claude Code wrote from the conversation (its ai-title)') + '. capcom shows it as a label and keeps nothing else of the conversation.' }) : null,
        h('div.chips', null, chips)),
      h('div.costbig', null, h('span.label', { text: 'API-equivalent cost' }),
        h('span.v', { text: lowerMark(t) + usd(t.usd), title: t.estimated_output.requests ? LOWER_TITLE : null }),
        t.estimated_output.requests ? h('span.note', { text: 'a lower bound: output partly estimated' }) : null,
        d.calibration ? calLine(d.calibration) : null))));

  main.append(h('div.grid', { style: '--min:180px' }, CATEGORY_ORDER.map((k) => {
    const c = t.categories[k];
    const value = k === 'output' ? outputUsd(t) : usd(c.usd);
    return h('section.card.tight', null,
      h('span.label', { style: 'display:flex;align-items:center;gap:8px' }, h('span.sw', { style: `background:${categoryColor(k)}` }), CATEGORY_LABELS[k]),
      h('span.big', { style: 'font-size:30px', text: value }),
      h('span.note', { text: `${tokens(c.tokens)} tokens · ${pct(t.usd ? c.usd / t.usd : 0)} of the cost` }));
  })));

  main.append(unfolded(d, r));

  const why = h('section.card.grow3', null, h('h2', { text: 'Why it cost this much' }));
  why.append(...whyItCost(d.spikes));
  const helped = h('section.card.grow2b', null, h('h2', { text: 'What would have helped' }),
    empty(null, 'Setup checks look at all your sessions together, ranked by what each change would save in a week.'),
    h('a.link', { href: '/checks', text: 'See the checks' }));
  main.append(h('div.row-wrap', { style: 'align-items:flex-start' }, why, helped));

  main.append(agentsTable(d));

  if (d.calibration) main.append(calFold(d.calibration, t));
}

// The session's spikes, largest first, each with its main cause when capcom.explain
// gave one (else what capcom.timeline saw).
function whyItCost(sp) {
  if (!sp) return [timelineMissing('Spike detection')];
  if (!sp.count) return [empty(null, 'No spikes: the spend rose at its usual pace. The cost is the sum of its turns, shown above request by request.')];
  const out = sp.items.map((it) => {
    const txt = spikeText(it);
    const cause = it.explanations && it.explanations.length ? it.explanations.reduce((a, b) => (b.excess_usd > a.excess_usd ? b : a)) : null;
    const thread = it.scope === 'session' ? 'session-wide' : it.agent_id ? `subagent${it.agent_type ? ' ' + it.agent_type : ''}` : 'main thread';
    return h('a.spikerow', { href: spikeHref(it.key, it.start) },
      h('span.num', { class: txt.unexplained ? 'num gray' : 'num', text: '+' }),
      h('span.body', null,
        h('span.meta', { text: `${stamp(new Date(it.start))} · ${thread}` }),
        h('b', { text: txt.title }),
        cause && cause.summary ? h('span.note', { text: cause.summary }) : null,
        h('span.tags', null, h('span.tag', { text: txt.tag }),
          h('span.plus', { text: `+${usd(it.excess_usd)}` }),
          h('span.note', { text: `${it.requests} request${it.requests === 1 ? '' : 's'}, ${usd(it.cost_usd)} in all` }))));
  });
  const more = sp.count > sp.items.length ? `${sp.count} spikes in all; the ${sp.items.length} largest are listed. ` : '';
  const why = sp.explained ? '' : 'Their causes are explained from milestone 3.';
  out.push(h('p.note', { style: 'margin:8px 0 0', text: `+${usd(sp.excess_usd)} over the usual pace. ${more}${why}` }));
  return out;
}

// The models chip: each model once, the first two named (all of them on hover),
// and the effort as the one most used, with every level's requests on hover.
function modelChip(sn) {
  const models = [...new Set(sn.models)];
  if (!models.length) return null;
  const counts = sn.effort_counts || Object.fromEntries((sn.efforts || []).map((e) => [e, 0]));
  const efforts = Object.keys(counts);
  let effort = '';
  if (efforts.length === 1) effort = ` · ${efforts[0]} effort`;
  else if (efforts.length > 1) effort = ` · mostly ${efforts[0]} effort`;
  const more = models.length > 2 ? ` +${models.length - 2}` : '';
  const tip = [models.length > 2 ? `Models: ${models.join(', ')}` : '',
    efforts.length > 1 ? 'Requests per effort: ' + efforts.map((e) => `${e} ${counts[e].toLocaleString('en-US')}`).join(', ') : ''].filter(Boolean).join('\n');
  return h('span.chip', { text: models.slice(0, 2).join(', ') + more + effort, title: tip || null });
}

// ---------------------------------------------------------------- cost check
// capcom's total against Claude Code's own estimate (its cost-state): one quiet
// line under the cost, and the parts of the difference in a fold at the bottom.

const CAL_TARGET = 0.02; // capcom.cost.calibrate TARGET_SPREAD: totals this close match
const CAL_SAME = 0.005; // amounts below half a cent are nothing

// match: within 2 %. known: further apart, and the parts explain it, or the
// session has calls the log cannot show (side calls, summaries), which the rest
// may be. problem: nothing like that, and still more than 2 % unexplained.
function calVerdict(c) {
  if (c.spread === null) return { kind: 'known', text: 'Claude Code counted nothing.' };
  const d = Math.abs(c.spread);
  if (d <= CAL_TARGET) return { kind: 'match', text: d >= 0.001 ? `It matches capcom's (${pct(d, 1)} apart).` : "It matches capcom's." };
  const by = `capcom's is ${pct(d)} ${c.spread < 0 ? 'lower' : 'higher'}`;
  const rest = Math.abs(c.rest_usd) >= Math.max(CAL_SAME, CAL_TARGET * c.claude_code_usd) ? usd(Math.abs(c.rest_usd), 2) : null;
  if (!rest) return { kind: 'known', text: `${by}, for known reasons.` };
  if (!c.comparable) return { kind: 'known', text: `${by}, partly for known reasons (${rest} unexplained).` };
  return { kind: 'problem', text: `${by}, and capcom cannot explain ${rest} of it.` };
}

// "23 of 26 processes", "one process"
function procWords(p) {
  return p.processes > 1 ? `${p.process || 0} of ${p.processes} processes` : 'one process';
}

function openCalFold() {
  const fold = document.getElementById('cost-check');
  if (!fold) return;
  fold.open = true;
  fold.scrollIntoView({ behavior: 'smooth', block: 'start' });
  fold.querySelector('summary').focus({ preventScroll: true });
}

function calLine(c) {
  const v = calVerdict(c);
  const part = c.part && !c.part.whole_session ? ` for ${procWords(c.part)}` : '';
  return h('span.calline', null,
    h('span', { text: `Claude Code's own estimate: ${usd(c.claude_code_usd)}${part}.` }),
    h('span', null, h('span', { class: v.kind === 'problem' ? 'bad' : null, text: v.text }), ' ',
      h('button.linkbtn', { type: 'button', onclick: openCalFold, text: v.kind === 'match' ? 'Details' : 'Why?' })));
}

const signed = (x) => (x < 0 ? '− ' : '+ ') + usd(Math.abs(x), 2);

function calFold(c, t) {
  const v = calVerdict(c);
  const p = c.part || { whole_session: true };
  const mine = c.output_range || usd(c.capcom_usd, 2);
  const row = (what, note, amount, cls) => h('tr', { class: cls || null },
    h('td', null, what, note ? h('span.note.block', null, h('span', { style: 'font-size:0', text: ' (' }), note, h('span', { style: 'font-size:0', text: ')' })) : null),
    h('td.num', { text: amount }));
  const rows = [row("capcom's count", null, (c.output_range ? '≥ ' : '') + usd(c.capcom_usd, 2))];
  if (c.output_above_usd >= CAL_SAME) rows.push(row('More output than capcom estimated', `the log has no true count, so capcom's is ${c.output_range}`, signed(c.output_above_usd)));
  if (c.unlogged_usd >= CAL_SAME) rows.push(row('Calls Claude Code saw that the log lacks', c.unlogged_mostly, signed(c.unlogged_usd)));
  if (c.logged_more_usd >= CAL_SAME) rows.push(row("Calls in the log that Claude Code didn't count", c.logged_more_mostly, signed(-c.logged_more_usd)));
  if (Math.abs(c.rest_usd) >= CAL_SAME) rows.push(row('Unexplained', null, signed(c.rest_usd), v.kind === 'problem' ? 'bad' : null));
  rows.push(row("Claude Code's estimate", null, usd(c.claude_code_usd, 2), 'sum'));

  const span = p.text ? p.text.split(', ')[0] : '';
  const lead = 'Claude Code keeps its own estimate of what a session costs. ' + (p.whole_session
    ? `It covers the whole session. ${v.text}`
    : `Here it covers ${procWords(p)} (${span}), and capcom counts the same part. ${v.text}`);
  const shown = new Set(['estimated output', 'calls not in the log']);
  const causes = (c.reasons || []).map((r, i) => [r, (c.reason_labels || c.reasons)[i]]).filter(([r]) => !shown.has(r) || (r === 'estimated output' && c.output_above_usd < CAL_SAME));
  const outside = (c.session_usd ?? c.capcom_usd) - c.capcom_usd;
  const others = (p.processes || 0) - (p.process || 0);
  const est = t.estimated_output;
  const notes = [
    outside >= CAL_SAME ? `Not in this check: ${usd(outside, 2)} in ${others > 1 ? `${others} processes` : 'a process'} that have not written an estimate. capcom's whole session: ${usd(c.session_usd, 2)}.` : null,
    est.requests ? `capcom estimates ${pct(est.share, est.share < 0.1 ? 1 : 0)} of output tokens from text length (${est.requests.toLocaleString('en-US')} requests), so its counts are lower bounds (≥).` : null,
    c.unlogged_usd >= CAL_SAME || c.output_above_usd >= CAL_SAME || c.logged_more_usd >= CAL_SAME ? 'The middle rows compare token counts and price the difference.' : null,
  ].filter(Boolean);

  return h('details.card.calfold', { id: 'cost-check' },
    h('summary', null, h('h2', { text: "Does this match Claude Code's estimate?" }),
      h('span.note', { class: v.kind === 'problem' ? 'note bad' : 'note', text: `Claude Code ${usd(c.claude_code_usd, 2)} · capcom ${mine}${c.spread === null ? '' : ` · ${pct(Math.abs(c.spread), 1)} apart`}` })),
    h('div.calbody', null,
      h('p', { text: lead }),
      h('table.data.narrow.calparts', null, h('tbody', null, rows)),
      causes.length ? h('div', null, h('p.note', { style: 'margin:0 0 4px', text: 'Why they cannot match exactly:' }),
        h('ul', null, causes.map(([, label]) => h('li', { text: label[0].toUpperCase() + label.slice(1) })))) : null,
      notes.map((n) => h('p.note', { style: 'margin:0', text: n }))));
}

// The timeline's plot is as wide as its active time, so a long session scrolls inside
// the card instead of squeezing into it, up to a cap that keeps the page light. A plot
// up to half again as wide as the room it has is squeezed into it instead (a short
// session fits a phone too), and any plot stretches to fill a wider room. Each squeezed
// pause takes a few pixels.
export const TL = { perMinute: 1.2, gap: 14, min: 240, max: 16000, squeeze: 1.5 };

export function plotWidth(activeMinutes, gaps) {
  const w = activeMinutes * TL.perMinute + gaps * TL.gap;
  return Math.round(Math.min(TL.max, Math.max(TL.min, w)));
}

// The narrowest the plot may be in `room` pixels: 0 when it fits there, else its width.
export function plotFloor(width, room) {
  return width <= room * TL.squeeze ? 0 : width;
}

// The time axis: active time at one scale across the plot, and each squeezed pause
// (over `minGap`) TL.gap pixels wide at every zoom, since it marks time left out rather
// than time (narrower only when that many would take over 70% of the plot). `axis(t,
// width)` is the time's x in pixels on a plot `width` wide, `axis.time(px, width)` the
// time back; `axis.gaps` are the squeezed pauses, `axis.activeMinutes` the rest.
export function gapPx(width, gaps) {
  return gaps ? Math.min(TL.gap, 0.7 * width / gaps) : 0;
}

export function timeAxis(start, end, pauses, minGap = 30 * 60e3) {
  const cut = pauses.filter((g) => g.end - g.start >= minGap && g.start >= start && g.end <= end);
  const active = Math.max(end - start - cut.reduce((a, g) => a + (g.end - g.start), 0), 1);
  const scale = (width) => {
    const g = gapPx(width, cut.length);
    return [g, (width - g * cut.length) / active];
  };
  const axis = (t, width) => {
    const [g, per] = scale(width);
    let acc = 0, prev = start;
    for (const c of cut) {
      if (t < c.start) break;
      acc += (c.start - prev) * per;
      if (t < c.end) return acc + (t - c.start) / (c.end - c.start) * g;
      acc += g; prev = c.end;
    }
    return Math.min(width, acc + (Math.max(t, prev) - prev) * per);
  };
  axis.time = (px, width) => {
    const [g, per] = scale(width);
    let acc = 0, prev = start;
    for (const c of cut) {
      const run = (c.start - prev) * per;
      if (px < acc + run) return prev + Math.max(px - acc, 0) / per;
      acc += run;
      if (px < acc + g) return c.start + (px - acc) / g * (c.end - c.start);
      acc += g; prev = c.end;
    }
    return Math.min(end, prev + Math.max(px - acc, 0) / per);
  };
  axis.gaps = cut;
  axis.activeMinutes = active / 60e3;
  return axis;
}

// Zoom: how much active time one screen of the plot shows. `hours` is a preset's (or a
// pinch's) hours per screen, or 'fit' for the whole session; `gaps` is the number of
// squeezed pauses (each its fixed pixels on top of the active time), `room` the plot's
// visible width. The width can come out under `room` (the session fits), where the
// presets are disabled; only the part in view is drawn, so any width can be scrolled.
export const ZOOMS = [['fit', 'Fit all'], [1, '1 h'], [4, '4 h'], [12, '12 h']];

export function zoomWidth(hours, activeMinutes, gaps, room) {
  if (hours === 'fit') return room;
  const a = room * activeMinutes / (hours * 60), g = gaps * TL.gap;
  return g <= 0.7 * (a + g) ? a + g : a / 0.3;
}

export function zoomHours(width, activeMinutes, gaps, room) {
  return activeMinutes / 60 * room / Math.max(width - gaps * gapPx(width, gaps), 1e-9);
}

// The closest zoom, in hours of active time per screen.
export const ZOOM_MIN = 0.25;

// The plot's width for a zoom, between `room` and the width at ZOOM_MIN (`max`).
export function zoomClamp(width, room, max) {
  return Math.min(Math.max(max, room), Math.max(room, width));
}

// The plot is drawn only around what is in view, so a session zoomed to an hour a
// screen keeps a small page however long it is: `drawWindow` is the pixels to draw,
// the `view` pixels scrolled to plus `margin` on either side; `sliceWindow` the indices
// [i0, i1) of the sorted times `ts` at x `px(t)` inside [a, b], one more on each side
// so a line runs on past the window's edges.
export function drawWindow(scrollLeft, view, width, margin = view) {
  return [Math.max(0, Math.min(scrollLeft, width) - margin), Math.min(width, scrollLeft + view + margin)];
}

export function sliceWindow(ts, px, a, b) {
  const first = (ok) => {
    let lo = 0, hi = ts.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (ok(px(ts[mid]))) hi = mid; else lo = mid + 1; }
    return lo;
  };
  return [Math.max(0, first((v) => v >= a) - 1), Math.min(ts.length, first((v) => v > b) + 1)];
}

// The scroll position that keeps the time `offset` pixels into the scroller (past
// the `lab` pixels of lane names) where it is when the plot on time axis `axis` goes
// from `from` to `to` pixels wide.
export function zoomScroll(scrollLeft, offset, lab, axis, from, to) {
  const t = axis.time(scrollLeft + offset - lab, from);
  return Math.max(0, lab + axis(t, to) - offset);
}

// The `zoom` query value: 'fit', or hours per screen ("1h", "2.5h"); null for none.
export function zoomParam(z) {
  return z === 'fit' ? 'fit' : typeof z === 'number' && z > 0 ? `${+z.toPrecision(2)}h` : null;
}

export function parseZoom(v) {
  if (v === 'fit') return 'fit';
  const m = /^(\d+(?:\.\d+)?)h$/.exec(v || '');
  return m && +m[1] > 0 ? +m[1] : null;
}

// Where the pause pills go on a plot `width` pixels wide, none over another: the
// longest pause first, each centered on its pause (kept inside the plot), on the first
// row with its whole length or else a short one, then on a second row the same way,
// and left out only when neither row has room. `pills` are {mid (0 to 1), minutes,
// full, short}; `textWidth(text)` is a label's width in pixels. Returns, in the order
// given, {text, left, row} (row 0 or 1) or null.
export function placePills(pills, width, textWidth, pad = 16, space = 4, rows = 2) {
  const placed = Array.from({ length: rows }, () => []);
  const out = pills.map(() => null);
  const order = pills.map((_, i) => i).sort((a, b) => pills[b].minutes - pills[a].minutes);
  for (const i of order) {
    const p = pills[i];
    fit: for (let row = 0; row < rows; row++) {
      for (const text of [p.full, p.short]) {
        const w = textWidth(text) + pad;
        const l = Math.min(Math.max(p.mid * width - w / 2, 0), Math.max(width - w, 0));
        if (placed[row].every(([a, b]) => l + w + space <= a || b + space <= l)) {
          placed[row].push([l, l + w]);
          out[i] = { text, left: l, row };
          break fit;
        }
      }
    }
  }
  return out;
}

// The spend rate lane: dollars per hour of active time, each request's cost spread
// over a triangle `window` wide on either side of it (so it reads like a moving
// 10-minute window, without its steps), sampled every `step`.
export const RATE = { window: 10 * 60e3, step: 2 * 60e3, minRun: 60e3 };

// The spend rate of the requests at times `t` costing `usd`, in $/hour, over the runs
// of active time between `start`, `end` and the squeezed `gaps` (sorted {start, end}):
// [{start, end, t: [...], rate: [...]}], one per run. A request counts only in its own
// run, its triangle cut at the run's edges and scaled back up, so the rate drops to
// zero across a pause instead of bridging it and each run's area is its cost. A run
// shorter than `minRun` is drawn that long. `pick(i)` says which requests count.
export function spendRate(t, usd, start, end, gaps, pick = () => true, opt = RATE) {
  const w = opt.window;
  const cdf = (u) => (u <= -w ? 0 : u < 0 ? (u + w) ** 2 / (2 * w * w) : u < w ? 1 - (w - u) ** 2 / (2 * w * w) : 1);
  const bounds = [];
  let prev = start;
  for (const g of gaps) { bounds.push([prev, g.start]); prev = g.end; }
  bounds.push([prev, end]);
  let i = 0;
  return bounds.map(([r0, r1]) => {
    r1 = Math.max(r1, r0 + opt.minRun);
    const own = [];
    while (i < t.length && t[i] <= r1) { if (t[i] >= r0 && pick(i)) own.push(i); i++; }
    const weight = own.map((k) => usd[k] / Math.max(cdf(r1 - t[k]) - cdf(r0 - t[k]), 1e-9));
    const n = Math.max(2, Math.ceil((r1 - r0) / opt.step) + 1);
    const ts = [], rate = [];
    let lo = 0;
    for (let j = 0; j < n; j++) {
      const u = r0 + (r1 - r0) * j / (n - 1);
      while (lo < own.length && t[own[lo]] < u - w) lo++;
      let v = 0;
      for (let k = lo; k < own.length && t[own[k]] <= u + w; k++) v += weight[k] * (1 - Math.abs(u - t[own[k]]) / w) / w;
      ts.push(u); rate.push(v * 3600e3);
    }
    return { start: r0, end: r1, t: ts, rate };
  });
}

// "39 min" -> "39m", "4 h 46" -> "5 h": a pause's label when the whole one does not fit
function shortPause(minutes) {
  return minutes < 60 ? `${Math.round(minutes)}m` : minutes < 48 * 60 ? `${Math.round(minutes / 60)} h` : `${Math.round(minutes / 1440)} d`;
}

// The x labels, chosen for the plot's width `W` so none overlaps: the session's
// end and start, each resume after a squeezed pause, then clock times at a step that
// leaves room between them (dates at midnight).
function axisLabels(start, end, x, W) {
  const runs = [];
  let prev = start;
  for (const g of x.gaps) { runs.push([prev, g.start]); prev = g.end; }
  runs.push([prev, end]);
  const activeMin = runs.reduce((a, [p, q]) => a + (q - p), 0) / 60e3;
  const perMin = (W * (x(end) - x(start) - x.gaps.reduce((a, g) => a + x(g.end) - x(g.start), 0))) / Math.max(activeMin, 1e-9);
  const step = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880].find((m) => m * perMin >= 110) || 2880;
  const marks = [{ t: end, label: stamp(new Date(end)) }, { t: start, label: stamp(new Date(start)) }];
  for (const g of x.gaps) marks.push({ t: g.end, label: stamp(new Date(g.end)) });
  const ms = step * 60e3;
  for (const [p, q] of runs) {
    const off = new Date(p).getTimezoneOffset() * 60e3;
    for (let t = Math.ceil((p - off) / ms) * ms + off; t < q; t += ms) {
      const dt = new Date(t);
      marks.push({ t, label: dt.getHours() || dt.getMinutes() ? hm(dt) : monthDay(dt) });
    }
  }
  const placed = [];
  const out = [];
  for (const m of marks) {
    const w = m.label.length * 6.4 + 4, c = x(m.t) * W;
    const l = c < 40 ? c : c > W - 40 ? c - w : c - w / 2;
    if (placed.every(([a, b]) => l + w + 12 <= a || b + 12 <= l)) { placed.push([l, l + w]); out.push(m); }
  }
  return out.sort((a, b) => a.t - b.t);
}

const perHour = (v) => `${usd(v, v >= 100 ? 0 : 2)}/h`;

// The spend rate lane under the context plot: $/hour on the same active-time axis,
// zero across each squeezed pause. Split into the main thread and the subagents
// (stacked, in their colors) when the session has both, else one curve.
function rateLane(d, R, start, end, x, X, at, draw, fitSvg, mainColor, hasMain, gapBands, spot) {
  const col = R.usd_all || R.usd;
  const split = hasMain && R.agent.some((a) => a >= 0);
  const low = spendRate(R.t, col, start, end, x.gaps, split ? (i) => R.agent[i] < 0 : () => true);
  const high = split ? spendRate(R.t, col, start, end, x.gaps, (i) => R.agent[i] >= 0) : null;
  const top = low.map((run, k) => run.rate.map((v, j) => v + (high ? high[k].rate[j] : 0)));
  const ticks = niceTicks(Math.max(...top.flat(), 1e-6), 2);
  const rmax = ticks[ticks.length - 1];
  const H = 110;
  const Y = (v) => (H - v / rmax * (H - 4)).toFixed(1);
  // only the samples in the drawn window [a, b]: within a run the axis is linear at the
  // same pixels per minute in every run, so each run's area keeps in step with its cost
  // at any zoom
  const seen = ([a, b]) => low.map((run, k) => [run, k, ...sliceWindow(run.t, at, a, b)]).filter(([, , i0, i1]) => i1 > i0);
  const trace = (run, vals, i0, i1) => run.t.slice(i0, i1).map((tm, j) => `L${X(tm)} ${Y(vals[i0 + j])}`).join('');
  const back = (run, vals, i0, i1) => run.t.slice(i0, i1).map((tm, j) => `L${X(tm)} ${Y(vals[i0 + j])}`).reverse().join('');
  const edge = (run, i) => X(run.t[i]);
  const lowLine = (w) => seen(w).map(([run, , i0, i1]) => `M${edge(run, i0)} ${H}${trace(run, run.rate, i0, i1)}L${edge(run, i1 - 1)} ${H}`).join('');
  const topLine = (w) => seen(w).map(([run, k, i0, i1]) => `M${edge(run, i0)} ${H}${trace(run, top[k], i0, i1)}L${edge(run, i1 - 1)} ${H}`).join('');
  const band = (w) => seen(w).map(([run, k, i0, i1]) => `M${trace(run, top[k], i0, i1).slice(1)}${back(run, run.rate, i0, i1)}Z`).join('');
  const line = (dd, color) => draw(s('path', { fill: 'none', 'stroke-width': 1.5, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke', style: `stroke:${color}` }), (w) => ({ d: dd(w) }));
  const svg = fitSvg(s('svg', { preserveAspectRatio: 'none', 'aria-hidden': 'true' },
    draw(s('path', { style: `fill:${mainColor};fill-opacity:0.28` }), (w) => ({ d: lowLine(w) })),
    split ? draw(s('path', { style: 'fill:var(--s2);fill-opacity:0.32' }), (w) => ({ d: band(w) })) : null,
    line(lowLine, mainColor),
    split ? line(topLine, 'var(--s2)') : null), H);
  const chart = h('div.chart', { style: `height:${H}px;border-bottom:1px solid var(--l15)` }, gapBands(), spot());
  const lab = h('div.lab.ctxlab', null, h('b', { text: 'Spend rate' }), h('span', { text: '$/hour, 10-min window' }));
  for (const v of ticks.slice(1)) {
    const at = (1 - v / rmax * (H - 4) / H) * 100;
    chart.append(h('div.gridline', { style: `top:${at.toFixed(2)}%` }));
    if (at >= 45) lab.append(h('span.ylab', { style: `top:${at.toFixed(2)}%`, text: perHour(v) }));
  }
  chart.append(svg);

  // hover: the rate at that moment, or the pause it falls in
  const samples = low.flatMap((run, k) => run.t.map((tm, j) => ({ t: tm, low: run.rate[j], high: high ? high[k].rate[j] : 0 })))
    .sort((a, b) => a.t - b.t);
  chart.addEventListener('mousemove', (e) => {
    if (chart.closest('.tlscroll.dragging')) return;
    const rect = chart.getBoundingClientRect();
    const f = (e.clientX - rect.left) / rect.width;
    const g = x.gaps.find((p) => f > x(p.start) + 1e-4 && f < x(p.end));
    if (g) { showTip(e, `Pause of ${duration((g.end - g.start) / 1e3)}`, [{ label: 'Spend rate', value: perHour(0) }]); return; }
    let lo = 0, hi = samples.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (x(samples[mid].t) < f) lo = mid + 1; else hi = mid; }
    if (lo > 0 && f - x(samples[lo - 1].t) <= x(samples[lo].t) - f) lo--;
    const p = samples[lo];
    showTip(e, `${stamp(new Date(p.t))} · spend rate`, [
      { label: 'Rate', value: perHour(p.low + p.high) },
      ...(split ? [{ label: 'Main thread', value: perHour(p.low) }, { label: 'Subagents', value: perHour(p.high) }] : []),
      { label: 'Over', value: '10-min window' },
    ]);
  });
  chart.addEventListener('mouseleave', hideTip);
  return { lab, chart, split };
}

const NOTE = 'Active time; pauses over 30 minutes are squeezed to a mark.';

function unfolded(d, r) {
  const card = h('section.card', null, h('div.split', null, h('h2', { text: 'How the session unfolded' }),
    h('span.note', { text: NOTE + ' Hover for values.' })));
  const R = d.requests;
  if (d.thinned) {
    card.append(h('p.note', { style: 'margin:0', text: `Showing ${d.thinned.kept.toLocaleString('en-US')} of ${d.thinned.requests.toLocaleString('en-US')} requests: in each run of neighbors of a thread, the costliest and the largest and smallest context, plus every request of a spike. Totals count them all.` }));
  }
  if (!R.t.length) { card.append(empty(null, 'No model requests in this session.')); return card; }
  const start = R.t[0], end = Math.max(R.t[R.t.length - 1], start + 60e3);
  const ax = timeAxis(start, end, d.pauses);
  const cut = ax.gaps, activeMin = ax.activeMinutes;
  const W = plotWidth(activeMin, cut.length);
  // the plot's width now: every mark is placed for it, and placed again when the plot
  // is laid out at another width (a zoom, a resize), since the pauses keep their pixels
  let PW = W;
  const x = (tm) => ax(tm, PW) / PW; // the fraction of the plot
  x.gaps = cut;
  const later = [];
  const place = (el, f) => { const set = () => Object.assign(el.style, f()); set(); later.push(set); return el; };
  // the charts' paths are drawn only across `win`, the pixels in view and a screen on
  // either side (all of the plot until it is laid out), and again as it scrolls
  let win = [0, PW];
  const windowed = [];
  const draw = (el, f) => { const set = () => { for (const [k, v] of Object.entries(f(win))) el.setAttribute(k, v); }; set(); windowed.push(set); return el; };
  const fitSvg = (svg, H) => draw(svg, ([a, b]) => ({ viewBox: `${a.toFixed(1)} 0 ${Math.max(b - a, 1).toFixed(1)} ${H}`, style: `left:${a.toFixed(1)}px;width:${Math.max(b - a, 1).toFixed(1)}px;right:auto` }));
  const pxAt = (tm) => ax(tm, PW);
  let zoom = parseZoom(r && r.query ? r.query.get('zoom') : null); // null: the default width
  const pc = (f) => (f * 100).toFixed(6) + '%'; // a hundredth of a pixel on the widest plot
  const X = (tm) => ax(tm, PW).toFixed(1);
  let xs = R.t.map((tm) => x(tm)); // never decreasing: the requests come in time order
  const relayout = (width) => {
    if (!width || Math.abs(width - PW) < 0.01) return false;
    PW = width;
    xs = R.t.map((tm) => x(tm));
    for (const f of later) f();
    return true;
  };
  const typeOf = (i) => (R.agent[i] < 0 ? 'main thread' : d.agents[R.agent[i]].agent_type);
  const nearest = (frac) => {
    let lo = 0, hi = xs.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (xs[mid] < frac) lo = mid + 1; else hi = mid; }
    return lo > 0 && frac - xs[lo - 1] <= xs[lo] - frac ? lo - 1 : lo;
  };
  const hover = (el) => {
    el.addEventListener('mousemove', (e) => {
      if (dragging) return;
      const rect = el.getBoundingClientRect();
      const i = nearest((e.clientX - rect.left) / rect.width);
      showTip(e, `${stamp(new Date(R.t[i]))} · ${typeOf(i)}`, [
        { label: 'Context', value: tokens(R.context[i]) },
        { label: 'Cost', value: usd(R.usd[i], R.usd[i] < 0.1 ? 3 : 2) },
        { label: 'Cache write', value: tokens(R.cache_write[i]) },
        { label: R.estimated[i] ? 'Output (estimated)' : 'Output', value: (R.estimated[i] ? '≥ ' : '') + tokens(R.output[i]) },
      ]);
    });
    el.addEventListener('mouseleave', hideTip);
    return el;
  };
  // the squeezed pauses, hatched across the charts; a spike a link pointed at, shaded
  const gapAt = (g) => () => ({ left: pc(x(g.start)), width: pc(x(g.end) - x(g.start)) });
  const gapBands = () => x.gaps.map((g) => place(h('div.tlgap'), gapAt(g)));
  const at = r && r.query ? Date.parse(r.query.get('at') || '') : NaN;
  const to = r && r.query ? Date.parse(r.query.get('to') || '') : NaN;
  const spot = () => (Number.isFinite(at) ? [place(h('div.tlspot'), () => ({ left: pc(x(at)), width: `max(4px, ${pc(Math.max(0, x(Number.isFinite(to) ? to : at) - x(at)))})` }))] : []);

  // context per request: the main thread as a line (in its lane's color), subagents as dots
  const mainColor = entityColor('main', 'agent');
  const main = R.t.map((_, i) => i).filter((i) => R.agent[i] < 0);
  const line = main.length ? main : R.t.map((_, i) => i);
  const ticks = niceTicks(maxOf(R.context, 1), 3);
  const cmax = ticks[ticks.length - 1];
  const H = 230;
  const Y = (v) => (H - v / cmax * H).toFixed(1);
  const lineT = line.map((i) => R.t[i]);
  const path = ([a, b]) => {
    const [i0, i1] = sliceWindow(lineT, pxAt, a, b);
    return [line.slice(i0, i1).map((i, k) => (k ? 'L' : 'M') + X(R.t[i]) + ' ' + Y(R.context[i])).join(''), i0, i1];
  };
  const area = ([p, i0, i1]) => (i1 > i0 ? p + `L${X(R.t[line[i1 - 1]])} ${H}L${X(R.t[line[i0]])} ${H}Z` : '');
  const ctx = h('div.chart', { style: `height:${H}px` }, gapBands(), spot());
  const ctxLab = h('div.lab.ctxlab', null, h('b', { text: 'Context per request' }), h('span', { text: 'tokens' }));
  for (const v of ticks.slice(1)) {
    const top = (1 - v / cmax) * 100;
    ctx.append(h('div.gridline', { style: `top:${top.toFixed(2)}%` }));
    if (top >= 28) ctxLab.append(h('span.ylab', { style: `top:${top.toFixed(2)}%`, text: tokens(v) }));
  }
  const svg = fitSvg(s('svg', { preserveAspectRatio: 'none', 'aria-hidden': 'true' },
    s('defs', null, s('linearGradient', { id: 'ctx-fill', x1: 0, y1: 0, x2: 0, y2: 1 },
      s('stop', { offset: 0, style: `stop-color:${mainColor};stop-opacity:0.30` }), s('stop', { offset: 1, style: `stop-color:${mainColor};stop-opacity:0.02` }))),
    s('path', { fill: 'url(#ctx-fill)' }),
    s('path', { fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke', style: `stroke:${mainColor}` })), H);
  const [areaEl, lineEl] = svg.querySelectorAll('path');
  draw(lineEl, (w) => { const p = path(w); areaEl.setAttribute('d', area(p)); return { d: p[0] }; });
  const dots = R.t.map((_, i) => i).filter((i) => R.agent[i] >= 0 && main.length);
  const dotT = dots.map((i) => R.t[i]);
  if (dots.length) {
    svg.append(draw(s('path', { fill: 'none', 'stroke-width': 3, 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--s2);opacity:0.55' }), ([a, b]) => {
      const [i0, i1] = sliceWindow(dotT, pxAt, a, b);
      return { d: dots.slice(i0, i1).map((i) => `M${X(R.t[i])} ${Y(R.context[i])}l0 0.01`).join('') };
    }));
  }
  ctx.append(svg);
  for (const c of d.compactions) {
    const mark = place(h('div', { class: c.agent_id ? 'tlcompact sub' : 'tlcompact' }), () => ({ left: pc(x(c.t)) }));
    ctx.append(tipOn(mark, `Compaction at ${stamp(new Date(c.t))}`, [
      { label: 'Thread', value: c.agent_id ? 'a subagent' : 'main thread' },
      { label: 'Trigger', value: c.trigger || 'unknown' },
      { label: 'Before', value: c.pre_tokens != null ? tokens(c.pre_tokens) : '?' },
      { label: 'After', value: c.post_tokens != null ? tokens(c.post_tokens) : '?' },
    ]));
  }
  hover(ctx);

  // the spend rate over active time, the main thread and the subagents stacked
  const cost = rateLane(d, R, start, end, x, X, pxAt, draw, fitSvg, mainColor, main.length > 0, gapBands, spot);

  // pauses: a tick for each; over the long ones a chip with its length, wherever it
  // fits at the plot's width (the longest first, shortened before it is dropped)
  const pauses = h('div.pauserow');
  const chips = [];
  for (const p of d.pauses) {
    const mid = () => (x(p.start) + x(p.end)) / 2;
    const title = `Pause of ${duration(p.minutes * 60)}, ${stamp(new Date(p.start))} to ${stamp(new Date(p.end))}`;
    const rows = [{ label: 'Next request', value: usd(p.next_usd) }, { label: 'Cache written', value: tokens(p.next_cache_write) }];
    pauses.append(tipOn(place(h('span', { class: p.minutes >= 60 ? 'pausetick warn' : 'pausetick' }), () => ({ left: pc(mid()) })), title, rows, { focus: false }));
    if (p.minutes >= 30) {
      const chip = tipOn(h('span', { class: p.minutes >= 60 ? 'pausechip warn' : 'pausechip', hidden: true }), title, rows);
      chips.push({ el: chip, get mid() { return mid(); }, minutes: p.minutes, full: duration(p.minutes * 60), short: shortPause(p.minutes) });
    }
  }
  pauses.append(...chips.map((c) => c.el));
  let measure = null;
  const placeChips = (width) => {
    if (!measure) {
      measure = document.createElement('canvas').getContext('2d');
      const cs = chips.length ? getComputedStyle(chips[0].el) : null;
      measure.font = `${cs ? cs.fontSize : '11px'} ${cs ? cs.fontFamily : 'sans-serif'}`;
    }
    const at = placePills(chips, width, (text) => measure.measureText(text).width);
    at.forEach((a, k) => {
      const el = chips[k].el;
      el.hidden = !a;
      if (!a) return;
      el.textContent = a.text;
      el.style.left = `${a.left.toFixed(1)}px`;
      el.style.transform = 'none';
      el.classList.toggle('row2', a.row === 1);
    });
    // a second line of pills only when one is on it
    pauses.classList.toggle('two', at.some((a) => a && a.row === 1));
  };

  // lanes: main thread, then one lane per agent type
  const lanes = [];
  const gapMarks = x.gaps.map((g) => place(h('div.gapmark', { style: 'border-left:0;background:var(--card)' }), gapAt(g)));
  // a bar at the very end stays inside the plot, so a plot that fits never scrolls
  const span = (el, a) => place(el, () => ({ left: `min(${pc(x(a.first))}, calc(100% - 3px))`, width: `max(3px, ${pc(Math.max(x(a.last) - x(a.first), 0))})` }));
  if (d.main) {
    lanes.push(h('div.lab', null, h('b', { text: 'Main thread' })),
      h('div.lane', { style: 'background:transparent' }, tipOn(span(h('span', { style: `background:${entityColor('main', 'agent')}` }), d.main),
        'Main thread', [{ label: 'Requests', value: String(d.main.requests) }, { label: 'Cost', value: usd(d.main.usd) }]), gapMarks));
  }
  const byType = new Map();
  d.agents.forEach((a) => { if (!byType.has(a.agent_type)) byType.set(a.agent_type, []); byType.get(a.agent_type).push(a); });
  const types = [...byType.entries()].sort((a, b) => b[1].reduce((s1, z) => s1 + z.usd, 0) - a[1].reduce((s1, z) => s1 + z.usd, 0));
  types.forEach(([type, list]) => {
    const color = entityColor(type, 'agent');
    const maxUsd = maxOf(list.map((a) => a.usd), 1e-9);
    const bar = (a) => tipOn(span(h('span', { style: `background:${color};opacity:${(0.45 + 0.55 * a.usd / maxUsd).toFixed(2)}` }), a),
      `${a.description || type} · ${hm(new Date(a.first))} to ${hm(new Date(a.last))}`, [
        { label: 'Requests', value: String(a.requests) }, { label: 'Tool calls', value: String(a.tool_calls) },
        { label: 'Peak context', value: tokens(a.peak_context) }, { label: 'Cost', value: usd(a.usd) }], { focus: false });
    if (list.length === 1) {
      const [a] = list;
      lanes.push(h('div.lab', null, h('span', null, h('b', { text: type }), ' ×1'),
        a.description ? h('span.what', { text: a.description, title: a.description }) : null),
      h('div.lane', null, bar(a)));
      return;
    }
    // several of one type share a lane; the toggle adds a row per agent, by its spawn description
    const shown = expandedTypes.has(type);
    const rows = list.flatMap((a, i) => {
      const name = a.description || `${type} ${i + 1}`;
      return [h('div.lab.sub', { hidden: !shown }, h('span.what', { text: name, title: name })),
        h('div.lane.sub', { hidden: !shown }, bar(a))];
    });
    const toggle = h('button.lanetoggle', { type: 'button', 'aria-expanded': String(shown), title: `Show each ${type} agent`, onclick: () => {
      const open = !expandedTypes.has(type);
      if (open) expandedTypes.add(type); else expandedTypes.delete(type);
      toggle.setAttribute('aria-expanded', String(open));
      for (const el of rows) el.hidden = !open;
    } }, s('svg', { width: 10, height: 10, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, s('path', { d: 'M9 5l7 7-7 7' })),
    h('span', null, h('b', { text: type }), ` ×${list.length}`));
    lanes.push(h('div.lab', null, toggle), h('div.lane', null, list.map(bar)), ...rows);
  });

  // x labels: the start, each resume after a squeezed pause, clock times, the end,
  // chosen for the plot's width once it is laid out
  const axis = thinned(h('div.xaxis'));
  const placeAxis = (width) => axis.replaceChildren(...axisLabels(start, end, x, width).filter((m) => {
    const c = x(m.t) * width;
    return c >= win[0] - 150 && c <= win[1] + 150; // only around the drawn window
  }).map((m) => {
    const f = x(m.t), c = f * width;
    const shift = c < 40 ? '0' : c > width - 40 ? '-100%' : '-50%';
    return h('span', { style: `left:${pc(f)};transform:translateX(${shift})`, text: m.label });
  }));

  // what the charts' marks are, only those it has: the context chart's, then the pauses'
  const item = (cls, style, text) => h('span', null, h('i' + cls, style ? { style } : null), text);
  const keys = [[
    main.length ? item('', `background:${mainColor}`, 'Main thread') : null,
    dots.length ? item('.tldot', null, 'Subagent requests') : null,
    cost.split ? item('', 'background:var(--s2)', 'Subagent spend') : null,
    d.compactions.some((c) => !c.agent_id) ? item('.tlkey.compact', null, 'Compaction') : null,
    d.compactions.some((c) => c.agent_id) ? item('.tlkey.compact.sub', null, 'Subagent compaction') : null,
    Number.isFinite(at) ? item('.tlkey.spot', null, 'The spike you followed') : null,
  ], [
    x.gaps.length ? item('.tlkey.gap', null, 'Pause over 30 min, squeezed') : null,
    d.pauses.length ? item('.tlkey.tick', null, 'Pause (5 min+)') : null,
    d.pauses.some((p) => p.minutes >= 60) ? item('.tlkey.tick.warn', null, 'Pause over an hour') : null,
  ]].map((g) => g.filter(Boolean)).filter((g) => g.length);
  if (keys.length) card.append(h('div.legend.tllegend', null, keys.map((g) => h('div.tlgroup', null, g))));

  const columns = (floor) => `var(--tllab) minmax(${floor}px, 1fr)`;
  const grid = h('div.tl', { style: `grid-template-columns:${columns(W)}` },
    ctxLab, ctx,
    cost.lab, cost.chart,
    h('div.lab', null, h('b', { text: 'Pauses' })), pauses,
    lanes,
    h('div.lab'), axis);
  const scroller = h('div.tlscroll', null, grid);

  // zoom: presets of active time per screen, ctrl+wheel (a trackpad's pinch) and a
  // touch pinch around the pointer; the plot's width changes and every lane, label and
  // pill follows it. The default (no zoom) is TL.perMinute pixels per active minute.
  const room = () => scroller.clientWidth - ctxLab.offsetWidth;
  const closest = (rm) => zoomWidth(ZOOM_MIN, activeMin, cut.length, rm);
  // draw the window again once the view leaves it (or always, after a zoom)
  // the moment at the view's left edge (or the end, when scrolled there), kept when
  // the card's width changes the plot's: a resize shows the same time, not the same pixels
  let anchor = null, seenWidth = 0;
  const hold = () => {
    if (scroller.clientWidth !== seenWidth) return; // a resize is pending: keep the last one
    const sl = scroller.scrollLeft;
    anchor = { t: ax.time(sl, PW), end: sl + room() >= PW - 2 };
  };
  const redraw = (force) => {
    const view = room(), sl = scroller.scrollLeft;
    const lo = Math.max(0, Math.min(sl, PW - view)), hi = Math.min(PW, sl + view);
    if (!force && win[0] <= lo + 0.5 && hi <= win[1] + 0.5) return;
    win = drawWindow(sl, view, PW);
    for (const f of windowed) f();
    placeAxis(PW);
    uncut();
  };
  // a plot that fits takes the room exactly (a max-content grid can come out a few
  // pixels wider and scroll for nothing)
  let opened = false, shownFloor = W;
  const applyFloor = (floor) => {
    shownFloor = floor;
    grid.style.gridTemplateColumns = columns(floor);
    grid.style.width = floor ? '' : '100%';
  };
  const floorFor = (rm) => {
    if (zoom === null) return plotFloor(W, rm);
    const w = zoom === 'fit' ? rm : zoomClamp(zoomWidth(zoom, activeMin, cut.length, rm), rm, closest(rm));
    return w <= rm ? 0 : Math.round(w);
  };
  const zoomButtons = ZOOMS.map(([z, label]) => h('button', { type: 'button', text: label, 'aria-pressed': 'false', onclick: (e) => {
    if (e.currentTarget.getAttribute('aria-disabled') === 'true') return;
    setZoom(z, ctxLab.offsetWidth + room() / 2);
  } }));
  const zoomBar = h('div.seg.tlzoom', { role: 'group', 'aria-label': 'Zoom: active time per screen' }, zoomButtons);
  const markZoom = () => {
    const rm = room();
    ZOOMS.forEach(([z], k) => {
      const b = zoomButtons[k];
      const w = zoomWidth(z, activeMin, cut.length, rm);
      const why = z === 'fit' ? null
        : w <= rm ? 'The whole session already fits at this zoom' : null;
      b.setAttribute('aria-disabled', String(!!why));
      b.title = why || (z === 'fit' ? 'The whole session in the card' : `${z} h of active time per screen`);
      b.setAttribute('aria-pressed', String(zoom === z && !why));
    });
  };
  let urlTimer = 0;
  const setZoom = (z, offset) => {
    zoom = z;
    const from = pauses.clientWidth || PW;
    const floor = floorFor(room());
    if (floor !== shownFloor) applyFloor(floor);
    const to = pauses.clientWidth || floor;
    const left = zoomScroll(scroller.scrollLeft, offset, ctxLab.offsetWidth, ax, from, to);
    relayout(to);
    scroller.scrollLeft = left;
    redraw(true);
    hold();
    markZoom();
    clearTimeout(urlTimer);
    urlTimer = setTimeout(() => {
      if (!scroller.isConnected) return;
      const q = new URLSearchParams(location.search);
      const v = zoomParam(zoom);
      if (v) q.set('zoom', v); else q.delete('zoom');
      const qs = q.toString();
      history.replaceState(history.state, '', location.pathname + (qs ? '?' + qs : ''));
    }, 250);
  };
  // zoom by a factor around a point `offset` pixels into the scroller
  const zoomBy = (factor, offset) => {
    const rm = room(), cur = pauses.clientWidth || PW;
    const w = zoomClamp(cur * factor, rm, closest(rm));
    if (Math.abs(w - cur) < 0.5) return;
    setZoom(w <= rm ? 'fit' : zoomHours(w, activeMin, cut.length, rm), offset);
  };
  scroller.addEventListener('wheel', (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    hideTip();
    const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    zoomBy(Math.exp(-Math.max(-60, Math.min(60, dy)) * 0.01), e.clientX - scroller.getBoundingClientRect().left);
  }, { passive: false });
  let pinch = null;
  const spread = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  scroller.addEventListener('touchstart', (e) => {
    pinch = e.touches.length === 2 ? spread(e.touches) : null;
  }, { passive: true });
  scroller.addEventListener('touchmove', (e) => {
    if (pinch === null || e.touches.length !== 2) return;
    e.preventDefault();
    const now = spread(e.touches);
    const mid = (e.touches[0].clientX + e.touches[1].clientX) / 2 - scroller.getBoundingClientRect().left;
    zoomBy(now / Math.max(pinch, 1), mid);
    pinch = now;
  }, { passive: false });
  scroller.addEventListener('touchend', () => { pinch = null; });
  card.append(zoomBar, scroller);

  // an x label or a pause pill partly under the sticky lane names is hidden, at every
  // scroll position, so none shows cut in half
  let frame = 0;
  const uncut = () => {
    const edge = ctxLab.getBoundingClientRect().right;
    const els = [...axis.children, ...chips.map((c) => c.el)].filter((el) => !el.hidden);
    const cut = els.map((el) => el.getBoundingClientRect().left < edge - 0.5);
    els.forEach((el, i) => el.classList.toggle('cut', cut[i]));
  };

  // opens at the end (the latest requests), or centered on the time a link points at
  if (typeof ResizeObserver !== 'undefined') {
    const watch = new ResizeObserver(() => {
      if (!scroller.isConnected) { if (opened) watch.disconnect(); return; }
      if (!scroller.clientWidth) return;
      const floor = floorFor(room());
      markZoom();
      if (floor !== shownFloor) applyFloor(floor);
      if (!pauses.clientWidth) return;
      const moved = relayout(pauses.clientWidth);
      if (moved && opened && anchor) scroller.scrollLeft = anchor.end ? scroller.scrollWidth : ax(anchor.t, PW);
      seenWidth = scroller.clientWidth;
      placeChips(pauses.clientWidth);
      redraw(moved || !opened);
      const scrolls = scroller.scrollWidth > scroller.clientWidth + 1;
      scroller.classList.toggle('scrolls', scrolls);
      card.querySelector('.split .note').textContent = NOTE + (scrolls ? ' Scroll sideways for the rest; ctrl+scroll or pinch to zoom; hover for values.' : ' Ctrl+scroll or pinch to zoom; hover for values.');
      if (opened) return;
      opened = true;
      const view = scroller.clientWidth - ctxLab.offsetWidth;
      scroller.scrollLeft = Number.isFinite(at)
        ? x(Number.isFinite(to) ? (at + Math.max(to, at)) / 2 : at) * pauses.clientWidth - view / 2
        : scroller.scrollWidth;
      hold();
      redraw(false);
      uncut();
    });
    watch.observe(scroller);
    watch.observe(pauses);
    scroller.addEventListener('scroll', () => {
      if (frame) return;
      frame = requestAnimationFrame(() => { frame = 0; hold(); redraw(false); uncut(); });
    }, { passive: true });
  } else {
    placeChips(W);
    placeAxis(W);
  }

  // a mouse drag on the plot scrolls it too (the wheel stays the page's, shift+wheel
  // and a trackpad's sideways swipe scroll the plot natively)
  let dragging = false;
  scroller.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'mouse' || e.button !== 0 || !scroller.classList.contains('scrolls')) return;
    if (!(e.target instanceof Element) || e.target.closest('.lab, button, a')) return;
    e.preventDefault();
    const x0 = e.clientX, s0 = scroller.scrollLeft;
    const move = (ev) => {
      if (!dragging && Math.abs(ev.clientX - x0) < 4) return;
      if (!dragging) { dragging = true; scroller.classList.add('dragging'); hideTip(); }
      scroller.scrollLeft = s0 - (ev.clientX - x0);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      dragging = false;
      scroller.classList.remove('dragging');
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  });
  return card;
}

function agentsTable(d) {
  const card = h('section.card', null, h('h2', { text: 'Agents in this session' }));
  if (!d.agent_types.length) { card.append(empty(null, 'No requests.')); return card; }
  card.append(h('div.scroll', null, h('table.data', null,
    h('thead', null, h('tr', null, ['Agent', 'Spawns', 'Tool calls (median)', 'Peak context (average)', 'Share of cost', 'Cost'].map((c, i) =>
      h('th', { scope: 'col', style: i && i !== 4 ? 'text-align:right' : null, text: c })))),
    h('tbody', null, d.agent_types.map((a) => {
      const color = entityColor(a.agent_type, 'agent');
      return h('tr', null,
        h('td', null, h('span', { style: 'display:flex;align-items:center;gap:8px;font-weight:500' }, h('span.sw', { style: `background:${color}` }), a.agent_type === 'main' ? 'main thread' : a.agent_type)),
        h('td.num', { text: a.spawns == null ? 'n/a' : String(a.spawns), style: a.spawns == null ? 'color:var(--muted)' : null }),
        // the main thread is one thread: its calls are a count, not a median
        h('td.num', { text: a.median_tool_calls == null ? `${a.tool_calls} in all` : String(a.median_tool_calls), style: a.median_tool_calls != null && a.median_tool_calls <= 2 ? 'color:var(--clay)' : null }),
        h('td.num', { text: tokens(a.average_peak_context) }),
        h('td', null, tipOn(h('div.track', { style: 'width:140px' }, h('div', { style: `width:${(a.share * 100).toFixed(1)}%;background:${color}` })),
          a.agent_type === 'main' ? 'main thread' : a.agent_type, [{ label: 'Share of cost', value: pct(a.share, 1), color }, { label: 'Cost', value: usd(a.usd) }])),
        h('td.cost', { text: usd(a.usd) }));
    })))));
  return card;
}
