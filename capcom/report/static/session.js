// Session view: cost and its check against Claude Code's own estimate, the
// per-request context and cost on an active-time axis, pauses, compactions, the
// main thread and the subagents as lanes, and the agents table.

import {
  h, s, api, usd, tokens, pct, duration, stamp, hm, monthDay, empty, tipOn, showTip, hideTip,
  outputUsd, lowerMark, LOWER_TITLE, CATEGORY_LABELS, CATEGORY_ORDER, categoryColor, entityColor, maxOf,
  spikeText, spikeHref, sessionDot,
} from './lib.js';
import { niceTicks, activeAxis, thinned, bandChart } from './charts.js';
import { timelineMissing } from './spend.js';
import { modelName } from './agent.js';

const agentHref = (sid, aid) => `/session/${encodeURIComponent(sid)}/agent/${encodeURIComponent(aid)}`;
const OPEN_NOTE = 'Click to open its timeline';

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
        h('div.chips', null, chips)))));

  main.append(costCard(d));

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

  if (d.agents.length || d.main) main.append(agentLives(d));
  main.append(agentsTable(d));

  if (d.calibration) main.append(calFold(d.calibration, t));
}

// ---------------------------------------------------------------- cost card
// The API-equivalent cost: on the left the cost adding up per category on an
// active-time axis (pauses of 30 minutes or more squeezed, all of them together to
// at most a tenth of the width, as the line is flat there), drawn like Spend's
// chart; on the right the number with its notes and the cost check's quiet line.
// On a phone the number comes first.

function costCard(d) {
  const t = d.totals;
  const side = h('div.costside', null, h('span.label', { text: 'API-equivalent cost' }),
    h('span.v', { text: lowerMark(t) + usd(t.usd), title: t.estimated_output.requests ? LOWER_TITLE : null }),
    t.estimated_output.requests ? h('span.note', { text: 'a lower bound: output partly estimated' }) : null,
    d.calibration ? calLine(d.calibration) : null);
  const c = d.cost_series;
  if (!c || !c.t.length) return h('section.card.costcard.alone', null, side);
  const start = c.t[0], end = Math.max(c.t[c.t.length - 1], start + 60e3);
  const long = d.pauses.filter((p) => p.end - p.start >= 30 * 60e3).length;
  const axis = activeAxis(start, end, d.pauses, 30 * 60e3, Math.min(0.012, 0.1 / Math.max(long, 1)));
  const members = CATEGORY_ORDER.filter((k) => c[k] && c[k][c[k].length - 1] > 0)
    .map((k) => ({ key: k, label: CATEGORY_LABELS[k], color: categoryColor(k), values: c[k] }))
    .sort((a, b) => b.values[b.values.length - 1] - a.values[a.values.length - 1]);
  const total = c.t.map((_, i) => members.reduce((a, m) => a + m.values[i], 0));
  // x labels: the start, the end, and between them the resumes after a squeezed
  // pause nearest a quarter, half and three quarters of the width (the start and
  // end always show, the others hide where they would touch); a session with no
  // such pause gets its hours
  const ticks = [{ t: start, label: stamp(new Date(start)), keep: true }];
  if (axis.gaps.length) {
    const picked = new Set();
    for (const q of [0.25, 0.5, 0.75]) {
      let best = null;
      for (const g of axis.gaps) if (!best || Math.abs(axis(g.end) - q) < Math.abs(axis(best.end) - q)) best = g;
      if (Math.abs(axis(best.end) - q) < 0.125) picked.add(best);
    }
    ticks.push(...[...picked].sort((a, b) => a.end - b.end).map((g) => ({ t: g.end, label: stamp(new Date(g.end)) })));
  } else for (let k = 1; k < 4; k++) ticks.push({ t: start + (end - start) * k / 4, label: hm(new Date(start + (end - start) * k / 4)) });
  ticks.push({ t: end, label: stamp(new Date(end)), keep: true });
  const chart = bandChart({ times: c.t, members, total, start, end, axis, ticks, height: 150, left: 48, endLabel: false });
  return h('section.card.costcard', null,
    h('div.costplot', null, h('span.label', null, 'How it added up',
      axis.gaps.length ? h('span.note', { text: ' · long pauses squeezed', title: 'Active time: each pause of 30 minutes or more takes almost no width.' }) : null), chart),
    side);
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
// short: the same for the quiet line under the cost, which names no estimate first.
function calVerdict(c) {
  if (c.spread === null) return { kind: 'known', text: 'Claude Code counted nothing.', short: "Claude Code's own estimate counted nothing." };
  const d = Math.abs(c.spread);
  if (d <= CAL_TARGET) return { kind: 'match', text: d >= 0.001 ? `It matches capcom's (${pct(d, 1)} apart).` : "It matches capcom's.", short: "This matches Claude Code's own estimate." };
  const dir = `${pct(d)} ${c.spread < 0 ? 'lower' : 'higher'}`;
  const by = `capcom's is ${dir}`, shortBy = `capcom's estimate is ${dir} than Claude Code's own estimate`;
  const rest = Math.abs(c.rest_usd) >= Math.max(CAL_SAME, CAL_TARGET * c.claude_code_usd) ? usd(Math.abs(c.rest_usd), 2) : null;
  const why = !rest ? ', for known reasons.' : !c.comparable ? `, partly for known reasons (${rest} unexplained).` : `, and capcom cannot explain ${rest} of it.`;
  return { kind: rest && c.comparable ? 'problem' : 'known', text: by + why, short: shortBy + why };
}

// Claude Code writes its estimate (a cost-state) once per process, that is per run
// of the claude CLI on the session: each start or resume. part.process counts the
// runs that wrote one, part.processes adds one per stretch of requests outside them.
function runsLead(p, span) {
  const n = p.process || 0, all = p.processes || n;
  if (p.whole_session && all <= 1) return 'Here it covers the whole session.';
  if (p.whole_session) return `This session had ${all} runs and it saved one in each, so it covers the whole session.`;
  return `This session had ${all} runs and it saved one in ${n} of them, so capcom compares only those ${n}${span ? ` (${span})` : ''}.`;
}

function openCalFold() {
  const fold = document.getElementById('cost-check');
  if (!fold) return;
  fold.open = true;
  fold.scrollIntoView({ behavior: 'smooth', block: 'start' });
  fold.querySelector('summary').focus({ preventScroll: true });
}

// One small line: how capcom's count compares with Claude Code's own estimate
// (which runs it covers is in the fold).
function calLine(c) {
  const v = calVerdict(c);
  return h('span.calline', null, h('span', { class: v.kind === 'problem' ? 'bad' : null, text: v.short }), ' ',
    h('button.linkbtn', { type: 'button', onclick: openCalFold, text: v.kind === 'match' ? 'Details' : 'Why?' }));
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
  const lead = `Claude Code saves its own estimate of what a session costs, once per run (each time claude starts or resumes the session). ${runsLead(p, span)} ${v.text}`;
  const shown = new Set(['estimated output', 'calls not in the log']);
  const causes = (c.reasons || []).map((r, i) => [r, (c.reason_labels || c.reasons)[i]]).filter(([r]) => !shown.has(r) || (r === 'estimated output' && c.output_above_usd < CAL_SAME));
  const outside = (c.session_usd ?? c.capcom_usd) - c.capcom_usd;
  const others = (p.processes || 0) - (p.process || 0);
  const est = t.estimated_output;
  const notes = [
    outside >= CAL_SAME ? `Not in this check: ${usd(outside, 2)} in ${others > 1 ? `${others} runs` : 'a run'} with no estimate saved (a run that is still going saves it later). capcom's whole session: ${usd(c.session_usd, 2)}.` : null,
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

// Where the pause pills go on a plot `width` pixels wide, none over another: the
// longest pause first, each centered on its pause (kept inside the plot), with its
// whole length or else a short one, and left out when neither fits. `pills` are
// {mid (0 to 1), minutes, full, short}; `textWidth(text)` is a label's width in pixels.
// Returns, in the order given, {text, left} or null.
export function placePills(pills, width, textWidth, pad = 16, space = 4) {
  const placed = [];
  const out = pills.map(() => null);
  const order = pills.map((_, i) => i).sort((a, b) => pills[b].minutes - pills[a].minutes);
  for (const i of order) {
    const p = pills[i];
    for (const text of [p.full, p.short]) {
      const w = textWidth(text) + pad;
      const l = Math.min(Math.max(p.mid * width - w / 2, 0), Math.max(width - w, 0));
      if (placed.every(([a, b]) => l + w + space <= a || b + space <= l)) {
        placed.push([l, l + w]);
        out[i] = { text, left: l };
        break;
      }
    }
  }
  return out;
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
  const cut = activeAxis(start, end, d.pauses).gaps;
  const activeMin = (end - start - cut.reduce((a, g) => a + g.end - g.start, 0)) / 60e3;
  const W = plotWidth(activeMin, cut.length);
  const x = activeAxis(start, end, d.pauses, 30 * 60e3, Math.min(0.012, TL.gap / W));
  const pc = (f) => (f * 100).toFixed(3) + '%';
  const X = (tm) => (x(tm) * W).toFixed(1);
  const xs = R.t.map((tm) => x(tm)); // never decreasing: the requests come in time order
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
  const gapBands = () => x.gaps.map((g) => h('div.tlgap', { style: `left:${pc(x(g.start))};width:${pc(x(g.end) - x(g.start))}` }));
  const at = r && r.query ? Date.parse(r.query.get('at') || '') : NaN;
  const to = r && r.query ? Date.parse(r.query.get('to') || '') : NaN;
  const spot = () => (Number.isFinite(at) ? [h('div.tlspot', { style: `left:${pc(x(at))};width:max(4px, ${pc(Math.max(0, x(Number.isFinite(to) ? to : at) - x(at)))})` })] : []);

  // context per request: the main thread as a line (in its lane's color), subagents as dots
  const mainColor = entityColor('main', 'agent');
  const main = R.t.map((_, i) => i).filter((i) => R.agent[i] < 0);
  const line = main.length ? main : R.t.map((_, i) => i);
  const ticks = niceTicks(maxOf(R.context, 1), 3);
  const cmax = ticks[ticks.length - 1];
  const H = 230;
  const Y = (v) => (H - v / cmax * H).toFixed(1);
  const path = line.map((i, k) => (k ? 'L' : 'M') + X(R.t[i]) + ' ' + Y(R.context[i])).join('');
  const area = path + `L${X(R.t[line[line.length - 1]])} ${H}L${X(R.t[line[0]])} ${H}Z`;
  const ctx = h('div.chart', { style: `height:${H}px` }, gapBands(), spot());
  const ctxLab = h('div.lab.ctxlab', null, h('b', { text: 'Context per request' }), h('span', { text: 'tokens' }));
  for (const v of ticks.slice(1)) {
    const top = (1 - v / cmax) * 100;
    ctx.append(h('div.gridline', { style: `top:${top.toFixed(2)}%` }));
    if (top >= 28) ctxLab.append(h('span.ylab', { style: `top:${top.toFixed(2)}%`, text: tokens(v) }));
  }
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' },
    s('defs', null, s('linearGradient', { id: 'ctx-fill', x1: 0, y1: 0, x2: 0, y2: 1 },
      s('stop', { offset: 0, style: `stop-color:${mainColor};stop-opacity:0.30` }), s('stop', { offset: 1, style: `stop-color:${mainColor};stop-opacity:0.02` }))),
    s('path', { d: area, fill: 'url(#ctx-fill)' }),
    s('path', { d: path, fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke', style: `stroke:${mainColor}` }));
  const dots = R.t.map((_, i) => i).filter((i) => R.agent[i] >= 0 && main.length);
  if (dots.length) {
    svg.append(s('path', { d: dots.map((i) => `M${X(R.t[i])} ${Y(R.context[i])}l0 0.01`).join(''), fill: 'none', 'stroke-width': 3, 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--s2);opacity:0.55' }));
  }
  ctx.append(svg);
  for (const c of d.compactions) {
    const mark = h('div', { class: c.agent_id ? 'tlcompact sub' : 'tlcompact', style: `left:${pc(x(c.t))}` });
    ctx.append(tipOn(mark, `Compaction at ${stamp(new Date(c.t))}`, [
      { label: 'Thread', value: c.agent_id ? 'a subagent' : 'main thread' },
      { label: 'Trigger', value: c.trigger || 'unknown' },
      { label: 'Before', value: c.pre_tokens != null ? tokens(c.pre_tokens) : '?' },
      { label: 'After', value: c.post_tokens != null ? tokens(c.post_tokens) : '?' },
    ]));
  }
  hover(ctx);

  // cost per request
  const cmaxUsd = maxOf(R.usd, 1e-6);
  const bars = (sel) => R.t.map((_, i) => i).filter(sel).map((i) => `M${X(R.t[i])} 80L${X(R.t[i])} ${(80 - R.usd[i] / cmaxUsd * 76).toFixed(1)}`).join('');
  const cost = hover(h('div.chart', { style: 'height:80px;border-bottom:1px solid var(--l15)' }, gapBands(), spot(),
    s('svg', { viewBox: `0 0 ${W} 80`, preserveAspectRatio: 'none', 'aria-hidden': 'true' },
      s('path', { d: bars((i) => R.agent[i] < 0), fill: 'none', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--muted)' }),
      s('path', { d: bars((i) => R.agent[i] >= 0), fill: 'none', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--s2)' }))));

  // pauses: a tick for each; over the long ones a chip with its length, wherever it
  // fits at the plot's width (the longest first, shortened before it is dropped)
  const pauses = h('div.pauserow');
  const chips = [];
  for (const p of d.pauses) {
    const mid = (x(p.start) + x(p.end)) / 2;
    const title = `Pause of ${duration(p.minutes * 60)}, ${stamp(new Date(p.start))} to ${stamp(new Date(p.end))}`;
    const rows = [{ label: 'Next request', value: usd(p.next_usd) }, { label: 'Cache written', value: tokens(p.next_cache_write) }];
    pauses.append(tipOn(h('span', { class: p.minutes >= 60 ? 'pausetick warn' : 'pausetick', style: `left:${pc(mid)}` }), title, rows, { focus: false }));
    if (p.minutes >= 30) {
      const chip = tipOn(h('span', { class: p.minutes >= 60 ? 'pausechip warn' : 'pausechip', hidden: true }), title, rows);
      chips.push({ el: chip, mid, minutes: p.minutes, full: duration(p.minutes * 60), short: shortPause(p.minutes) });
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
    placePills(chips, width, (text) => measure.measureText(text).width).forEach((at, k) => {
      const el = chips[k].el;
      el.hidden = !at;
      if (!at) return;
      el.textContent = at.text;
      el.style.left = `${at.left.toFixed(1)}px`;
      el.style.transform = 'none';
    });
  };

  // lanes: main thread, then one lane per agent type
  const lanes = [];
  const sid = d.session.session_id;
  const gapMarks = x.gaps.map((g) => h('div.gapmark', { style: `left:${pc(x(g.start))};width:${pc(x(g.end) - x(g.start))};border-left:0;background:var(--card)` }));
  const span = (a) => `left:${pc(x(a.first))};width:max(3px, ${pc(Math.max(x(a.last) - x(a.first), 0))})`;
  if (d.main) {
    lanes.push(h('div.lab', null, h('b', { text: 'Main thread' })),
      h('div.lane', { style: 'background:transparent' }, tipOn(h('a.lanebar', { 'data-mark': '', href: agentHref(sid, 'main'), style: `${span(d.main)};background:${entityColor('main', 'agent')}` }),
        'Main thread', [{ label: 'Requests', value: String(d.main.requests) }, { label: 'Cost', value: usd(d.main.usd) }, { note: OPEN_NOTE }],
        { native: true, aria: 'Main thread: open its timeline' }), gapMarks));
  }
  const byType = new Map();
  d.agents.forEach((a) => { if (!byType.has(a.agent_type)) byType.set(a.agent_type, []); byType.get(a.agent_type).push(a); });
  const types = [...byType.entries()].sort((a, b) => b[1].reduce((s1, z) => s1 + z.usd, 0) - a[1].reduce((s1, z) => s1 + z.usd, 0));
  types.forEach(([type, list]) => {
    const color = entityColor(type, 'agent');
    const maxUsd = maxOf(list.map((a) => a.usd), 1e-9);
    const bar = (a) => tipOn(h('a.lanebar', { 'data-mark': '', href: agentHref(sid, a.agent_id), style: `${span(a)};background:${color};opacity:${(0.45 + 0.55 * a.usd / maxUsd).toFixed(2)}` }),
      `${a.description || type} · ${type}${a.model ? ' · ' + modelName(a.model) : ''}`, [
        { label: `${hm(new Date(a.first))} to ${hm(new Date(a.last))}`, value: a.alive_seconds != null ? duration(a.alive_seconds) : '' },
        { label: 'Requests', value: String(a.requests) }, { label: 'Tool calls', value: String(a.tool_calls) },
        { label: 'Peak context', value: tokens(a.peak_context) }, { label: 'Cost', value: usd(a.usd) }, { note: OPEN_NOTE }],
      { native: true, aria: `${type} agent: open its timeline` });
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
  const placeAxis = (width) => axis.replaceChildren(...axisLabels(start, end, x, width).map((m) => {
    const f = x(m.t), c = f * width;
    const shift = c < 40 ? '0' : c > width - 40 ? '-100%' : '-50%';
    return h('span', { style: `left:${pc(f)};transform:translateX(${shift})`, text: m.label });
  }));

  // what the charts' marks are, only those it has: the context chart's, then the pauses'
  const item = (cls, style, text) => h('span', null, h('i' + cls, style ? { style } : null), text);
  const keys = [[
    main.length ? item('', `background:${mainColor}`, 'Main thread') : null,
    dots.length ? item('.tldot', null, 'Subagent requests') : null,
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
    h('div.lab', null, h('b', { text: 'Cost per request' }), h('span', { text: `scale $0 to ${usd(cmaxUsd)}` })), cost,
    h('div.lab', null, h('b', { text: 'Pauses' })), pauses,
    lanes,
    h('div.lab'), axis);
  const scroller = h('div.tlscroll', null, grid);
  card.append(scroller);

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
  let opened = false, shownFloor = W;
  if (typeof ResizeObserver !== 'undefined') {
    const watch = new ResizeObserver(() => {
      if (!scroller.isConnected) { if (opened) watch.disconnect(); return; }
      if (!scroller.clientWidth) return;
      const floor = plotFloor(W, scroller.clientWidth - ctxLab.offsetWidth);
      if (floor !== shownFloor) { shownFloor = floor; grid.style.gridTemplateColumns = columns(floor); }
      if (!pauses.clientWidth) return;
      placeChips(pauses.clientWidth);
      placeAxis(pauses.clientWidth);
      uncut();
      const scrolls = scroller.scrollWidth > scroller.clientWidth + 1;
      scroller.classList.toggle('scrolls', scrolls);
      card.querySelector('.split .note').textContent = NOTE + (scrolls ? ' Scroll sideways for the rest; hover for values.' : ' Hover for values.');
      if (opened) return;
      opened = true;
      const view = scroller.clientWidth - ctxLab.offsetWidth;
      scroller.scrollLeft = Number.isFinite(at)
        ? x(Number.isFinite(to) ? (at + Math.max(to, at)) / 2 : at) * pauses.clientWidth - view / 2
        : scroller.scrollWidth;
      uncut();
    });
    watch.observe(scroller);
    watch.observe(pauses);
    scroller.addEventListener('scroll', () => {
      if (frame) return;
      frame = requestAnimationFrame(() => { frame = 0; uncut(); });
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

// Each subagent's life, the longest first: when it was spawned, how long it lived,
// how much of it the model worked and its tools ran; each opens the agent's page.
const LIVES_SHOWN = 12; // agents listed before "All N agents"

function agentLives(d) {
  const sid = d.session.session_id;
  const list = [...d.agents].sort((a, b) => (b.alive_seconds || 0) - (a.alive_seconds || 0));
  const days = new Set(list.map((a) => (a.spawned ? new Date(a.spawned).toDateString() : '')));
  const when = (t) => { const x = new Date(t); return days.size > 1 ? `${monthDay(x)} ${hm(x)}` : hm(x); };
  const row = (a) => {
    const name = a.description || a.agent_type;
    const work = a.model_seconds || 0, tools = a.tool_seconds || 0, both = work + tools || 1;
    return h('tr', null,
      h('td', null, h('a.title', { href: agentHref(sid, a.agent_id) }, h('span.privname', { text: name }),
        h('span', { text: `${a.agent_type}${a.model ? ' · ' + modelName(a.model) : ''}` }))),
      h('td', { style: 'font-family:var(--mono);font-size:12px;color:var(--muted)', text: a.spawned ? when(a.spawned) : '' }),
      h('td.num', { text: a.alive_seconds != null ? lifeText(a.alive_seconds) : '' }),
      h('td', null, h('div.agbar', { role: 'img', 'aria-label': `Model working ${pct(work / both)}, tools ${pct(tools / both)}` },
        tipOn(h('span', { style: `flex:${work.toFixed(1)} 1 0;background:var(--s1)` }), 'Model working', [{ label: lifeText(work), value: pct(work / both) }], { focus: false }),
        tipOn(h('span', { style: `flex:${tools.toFixed(1)} 1 0;background:var(--s3)` }), 'Tools running', [{ label: lifeText(tools), value: pct(tools / both) }], { focus: false }))),
      h('td.num', { text: String(a.requests) }),
      h('td.cost', { text: usd(a.usd) }));
  };
  const body = h('tbody');
  let all = false;
  const more = list.length > LIVES_SHOWN ? h('button.link.agmore', { type: 'button' }) : null;
  const fill = () => {
    body.replaceChildren(...(all ? list : list.slice(0, LIVES_SHOWN)).map(row));
    if (more) { more.textContent = all ? `Only the ${LIVES_SHOWN} longest` : `All ${list.length} agents`; more.setAttribute('aria-expanded', String(all)); }
  };
  if (more) more.addEventListener('click', () => { all = !all; fill(); });
  fill();
  // the lane bars of the timeline link the threads too, but they are thin marks: every
  // thread has a full-size link here, the main thread's beside the heading
  const mainLink = d.main ? h('a.agmain', { href: agentHref(sid, 'main'), text: "The main thread's timeline" }) : null;
  const card = h('section.card', null, h('div.aghrow', null, h('h2', { text: 'Agents in this session' }),
    h('span.note', null, list.length ? 'Sorted by time alive' : '', mainLink && list.length ? ' · ' : '', mainLink)));
  if (!list.length) { card.append(h('p.note', { text: 'No subagents: only the main thread ran in this session.' })); return card; }
  card.append(h('div.scroll', null, h('table.data', { style: 'min-width:720px' },
    h('thead', null, h('tr', null, ['Agent', 'Spawned', 'Alive', 'Model working · tools', 'Requests', 'Cost'].map((c, i) =>
      h('th', { scope: 'col', class: i === 2 || i >= 4 ? 'num' : null, style: i === 3 ? 'width:220px' : null, text: c })))),
    body)));
  card.append(h('div.aglegend', null, h('span', null, h('i', { style: 'background:var(--s1)' }), 'Model working'),
    h('span', null, h('i', { style: 'background:var(--s3)' }), 'Tools running')), more);
  return card;
}

function lifeText(sec) {
  if (sec < 60) return `${Math.round(sec)} s`;
  const m = Math.round(sec / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
}

function agentsTable(d) {
  const card = h('section.card', null, h('h2', { text: 'Agent types' }));
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
