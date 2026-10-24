// Agent page: one thread of a session (a subagent, or the main thread) from its
// spawn to its hand-back: what it did phase by phase and step by step, its context
// and spend over time, and where its time and cost went. Everything is derived on
// the server (capcom/report/agent.py); this draws it.

import { h, s, api, usd, usdShort, tokens, pct, duration, hm, two, monthDay, tipOn, showTip, hideTip, seg, segBar, empty, sessionHref, sessionDot } from './lib.js';
import { activeAxis, niceTicks } from './charts.js';

const KIND_ORDER = ['think', 'test', 'files', 'other', 'shot'];
const KIND_COLOR = { think: 'var(--s1)', test: 'var(--s2)', files: 'var(--s3)', other: 'var(--bar-before)', shot: 'var(--s5)' };
const KIND_DOING = { think: 'thinking and writing', test: 'running tests and builds', files: 'in files', other: 'in other commands', shot: 'taking screenshots' };
const IDLE_SQUEEZE = 5 * 60e3; // a wait for the user longer than this is squeezed on the axis
const ZOOMS = [['all', 'Fit all'], ['900', '15 min'], ['300', '5 min']];
const WAIT_NOTE = "The log has one start and one end for a command: it can't tell the work from a permission prompt or a queue.";
const ROWS_MAX = 3000; // steps listed at most in "All steps"

// the view's state, kept across redraws of one thread (a theme switch, a partial reply)
const view = { key: null, zoom: 'all', sel: null, all: false, scroll: 0, folds: new Map(), sid: '', aid: 'main' };

// "19 min 42 s", "8 s", "1 h 04"
function took(sec) {
  if (!(sec > 0)) return '0 s';
  if (sec < 1) return '<1 s';
  if (sec < 60) return `${Math.round(sec)} s`;
  if (sec < 3600) { const m = Math.floor(sec / 60); return `${m} min ${two(Math.round(sec - m * 60) % 60)} s`; }
  return duration(sec);
}
// "22 min", "1 h 24 min", "40 s"
function span(sec) {
  if (sec < 60) return `${Math.round(sec)} s`;
  const m = Math.round(sec / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h${m % 60 ? ` ${two(m % 60)} min` : ''}`;
}
const tile = (sec) => (sec < 60 ? `${Math.round(sec)} s` : duration(sec));

// a thread that runs over several days dates its times; one within a day gives only the clock
let multiDay = false;
const sameDay = (a, b) => new Date(a).toDateString() === new Date(b).toDateString();
// "17:30", or "Aug 6 17:30" in a thread of several days
function at(t) { const d = new Date(t); return multiDay ? `${monthDay(d)} ${hm(d)}` : hm(d); }
// "17:30 to 17:48", "Aug 6 17:30 to 17:48", "Aug 6 18:49 to Aug 7 11:59"
const fromTo = (a, b) => `${at(a)} to ${sameDay(a, b) ? hm(new Date(b)) : at(b)}`;

// "claude-opus-5-5" -> "Opus 5.5", "claude-3-5-haiku-20241022" -> "Haiku 3.5"
export function modelName(id) {
  if (!id) return 'unknown model';
  const words = id.replace(/^claude-/, '').replace(/-\d{8}$/, '').replace(/\[.*\]$/, '').split('-');
  const family = words.find((w) => /^[a-z]+$/i.test(w));
  const version = words.filter((w) => /^\d{1,2}$/.test(w)).join('.');
  if (!family) return id;
  return family[0].toUpperCase() + family.slice(1) + (version ? ' ' + version : '');
}

const NUMBERS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
const count = (n) => NUMBERS[n] || String(n);
const plural = (n, one, many) => `${count(n)} ${n === 1 ? one : many || one + 's'}`;
function share(x) {
  if (x > 0.5) return 'over half';
  if (x > 1 / 3) return 'over a third';
  if (x > 0.25) return 'over a quarter';
  return pct(x);
}

export async function renderAgent(main, r) {
  const sid = r.rest[0] || '';
  const aid = r.rest[2] || 'main';
  const d = await api(`agent/${encodeURIComponent(sid)}?agent=${encodeURIComponent(aid)}`);
  const key = sid + '/' + aid;
  if (view.key !== key) Object.assign(view, { key, zoom: 'all', sel: null, all: false, scroll: 0, folds: new Map(), sid, aid });
  const T = d.thread, st = d.steps, R = d.requests;
  const n = st.start.length;
  multiDay = !sameDay(T.first, T.last);
  const working = T.alive_seconds - (T.is_main ? T.idle_seconds : 0);

  main.append(header(d, sid));
  main.append(h('div.grid.agtiles', { style: '--min:150px' }, tiles(d, working).map((t) => h('section.card.agtile', null,
    h('span.label', { text: t.label }), h('span.v', { text: t.value }), h('span.note', { text: t.sub || '' })))));
  if (!n) { main.append(empty('No steps', 'This thread made no requests capcom could count.')); return; }

  const lower = h('div.aglower');
  const draw = () => {
    const old = lower.querySelector('.agscroll');
    // keep where the view was scrolled, unless a pick asked to scroll to the phase (-1)
    if (old && view.scroll !== -1) view.scroll = old.scrollLeft / Math.max(1, old.scrollWidth);
    lower.replaceChildren(timelineCard(d, draw), phasesCard(d, draw), h('div.row-wrap.agpair', null, timeCard(d, working), slowCard(d, draw)));
  };
  draw();
  main.append(lower);
}

// ------------------------------------------------------------------ header

function header(d, sid) {
  const T = d.thread, sn = d.session;
  const title = sn.title || 'Untitled session ' + sid.slice(0, 8);
  const agentHref = (x) => `${sessionHref(sid)}/agent/${encodeURIComponent(x.agent_id)}`;
  const step = (x, text) => (x ? h('a.agnav', { href: agentHref(x), text, title: `${text}: ${x.name}` }) : h('span.agnav.off', { text, 'aria-disabled': 'true' }));
  const first = new Date(T.first), last = new Date(T.last);
  const spanQ = `?at=${encodeURIComponent(first.toISOString())}&to=${encodeURIComponent(last.toISOString())}`;
  const sub = [];
  if (T.is_main) {
    sub.push('Main thread', modelName(T.model), multiDay ? fromTo(T.first, T.last) : `${monthDay(first)} ${hm(first)} to ${hm(last)}`);
  } else {
    sub.push(`${T.agent_type || 'unknown'} subagent`, modelName(T.model));
    const by = T.spawned_by ? (T.spawned_by.agent_id === 'main' ? 'the main thread' : h('span.privname', { text: T.spawned_by.name })) : null;
    const end = T.finished === false ? `last request ${at(T.last)}` : `handed back ${at(T.last)}`;
    sub.push(h('span', null, by ? ['spawned ', at(T.first), ' by ', by, ', ', end] : [`from ${at(T.first)}, `, end]));
  }
  sub.push(h('a', { href: sessionHref(sid) + spanQ, text: 'see it on the session timeline' }));
  const subline = h('div.agsub');
  sub.forEach((x, i) => { if (i) subline.append(' · '); subline.append(x); });
  return h('header.aghead', null,
    h('nav.crumb', { 'aria-label': 'Breadcrumb' },
      h('a', { href: '/sessions', text: 'Sessions' }), h('span', { 'aria-hidden': 'true', text: '/' }),
      h('a', { href: sessionHref(sid), text: title }), h('span', { 'aria-hidden': 'true', text: '/' }),
      h('span.privname', { text: T.name })),
    h('div.agtitle', null,
      h('h1.privname', null, sessionDot(sn.color), T.name),
      h('div.agnavs', null, step(d.neighbors.prev, 'Previous agent'), step(d.neighbors.next, 'Next agent'))),
    subline,
    summary(d));
}

// One paragraph in plain words: where its life went, what it cost, its slowest phases.
function summary(d) {
  const T = d.thread;
  const life = T.alive_seconds - (T.is_main ? T.idle_seconds : 0);
  const kinds = d.kinds.filter((k) => k.seconds >= 30).sort((a, b) => b.seconds - a.seconds);
  const parts = kinds.slice(0, 3).map((k) => `${span(k.seconds)} ${KIND_DOING[k.key]}`);
  if (kinds.length > 3) parts.push(`${span(kinds.slice(3).reduce((a, k) => a + k.seconds, 0))} in the rest`);
  const list = parts.length > 1 ? parts.slice(0, -1).join(', ') + ' and ' + parts[parts.length - 1] : parts[0] || '';
  const lead = T.is_main ? `${span(life)} working` : `${span(life)} alive`;
  const p = h('p.agsum', null, h('b', { text: lead + (list ? ':' : '.') }), list ? ` ${list}. ` : ' ');
  p.append(h('b', { text: usd(T.usd) }), ` over its ${plural(T.requests, 'request')}`);
  if (T.peak_context > T.start_context * 1.5) p.append(`, as its context grew from ${tokens(T.start_context)} to ${tokens(T.peak_context)} tokens`);
  const tools = d.kinds.filter((k) => k.key !== 'think');
  const toolSec = tools.reduce((a, k) => a + k.seconds, 0), toolUsd = tools.reduce((a, k) => a + k.usd, 0);
  if (life > 0 && toolSec / life > 0.3 && T.usd > 0 && toolUsd / T.usd < 0.25) p.append('; the waits on its tools cost little');
  p.append('. ');
  const slow = [...d.phases].sort((a, b) => b.seconds - a.seconds).slice(0, 2);
  if (d.phases.length >= 3 && slow.length === 2) {
    p.append('The two slowest phases were ', h('i.privname', { text: slow[0].name }), ` (${span(slow[0].seconds)}) and `,
      h('i.privname', { text: slow[1].name }), ` (${span(slow[1].seconds)}).`);
  }
  return p;
}

function tiles(d, working) {
  const T = d.thread;
  const life = Math.max(working, 1);
  const test = (d.kinds.find((k) => k.key === 'test') || { seconds: 0 }).seconds;
  const of = T.is_main ? 'of its working time' : 'of its life';
  const out = [];
  if (T.is_main) out.push({ label: 'Working', value: tile(working), sub: T.idle_seconds >= 60 ? `${duration(T.idle_seconds)} waiting for you` : fromTo(T.first, T.last) });
  else out.push({ label: 'Alive', value: tile(T.alive_seconds), sub: fromTo(T.first, T.last) });
  out.push({ label: 'Model working', value: tile(T.model_seconds), sub: `${pct(T.model_seconds / life)} ${of}` });
  out.push({ label: 'Tools and waits', value: tile(T.tool_seconds), sub: pct(T.tool_seconds / life) + (test >= 60 ? `, ${duration(test)} in tests` : '') });
  out.push({ label: 'Cost', value: usd(T.usd), sub: life >= 600 ? `${usd(T.usd / (life / 3600))} per hour ${T.is_main ? 'working' : 'alive'}` : '' });
  out.push({ label: 'Requests', value: T.requests.toLocaleString('en-US'), sub: T.requests > 1 ? `one every ${took(life / T.requests)}` : '' });
  out.push({ label: 'Peak context', value: tokens(T.peak_context), sub: `from ${tokens(T.start_context)} ${T.is_main ? 'at the start' : 'at spawn'}` });
  return out;
}

// ------------------------------------------------------------------ the timeline

// The axis: time to a fraction of the plot, with long waits for the user squeezed,
// and how many times wider than the view the plot is (the zoom).
function axis(d) {
  const T = d.thread;
  const gaps = d.idle.map(([a, b]) => ({ start: a, end: b }));
  // each squeezed wait a sliver, and all of them together at most an eighth of the axis
  const share = Math.min(0.012, 0.12 / Math.max(1, gaps.filter((g) => g.end - g.start >= IDLE_SQUEEZE).length));
  const x = activeAxis(T.first, T.last, gaps, IDLE_SQUEEZE, share);
  const removed = x.gaps.reduce((a, g) => a + (g.end - g.start), 0);
  const active = Math.max((T.last - T.first - removed) / 1000, 1);
  let k = 1;
  if (view.sel != null) {
    const p = d.phases[view.sel - 1];
    if (p) k = Math.min(60, Math.max(1, 1 / Math.max((x(p.end) - x(p.start)) * 1.25, 1e-4)));
  } else if (view.zoom !== 'all') {
    k = Math.min(200, Math.max(1, active / Number(view.zoom)));
  }
  if (k === 1) return { x, k, gaps };
  // zoomed in, a squeezed wait keeps about the width it had, not k times it; the
  // phase then takes more of the axis, so its zoom is worked out again on that axis
  let zx = activeAxis(T.first, T.last, gaps, IDLE_SQUEEZE, share / k);
  const p = view.sel != null ? d.phases[view.sel - 1] : null;
  if (p) {
    k = Math.min(60, Math.max(1, 1 / Math.max((zx(p.end) - zx(p.start)) * 1.25, 1e-4)));
    zx = activeAxis(T.first, T.last, gaps, IDLE_SQUEEZE, share / k);
  }
  return { x: zx, k, gaps }; // every wait is marked; only the long ones are squeezed
}

const pc = (f) => `${(f * 100).toFixed(3)}%`;

function timelineCard(d, draw) {
  const T = d.thread, st = d.steps, R = d.requests;
  const { x, k, gaps } = axis(d);
  const W = 1000;
  const X = (t) => (x(t) * W).toFixed(2);
  const sel = view.sel != null ? d.phases[view.sel - 1] : null;
  const gapMarks = () => gaps.map((g) => tipOn(h('div.aggap', { style: `left:${pc(x(g.start))};width:${pc(x(g.end) - x(g.start))}` }),
    `Waiting for you, ${span((g.end - g.start) / 1000)}`, [{ label: 'From', value: at(g.start) }, { label: 'To', value: at(g.end) }], { focus: false }));

  // what it did: one box per phase
  const phases = h('div.agphases', null, d.phases.map((p) => {
    const on = sel === p;
    const box = h('button', {
      type: 'button', class: 'agphase' + (on ? ' on' : '') + (p.guessed ? ' guessed' : ''),
      style: `left:${pc(x(p.start))};width:calc(${pc(Math.max(x(p.end) - x(p.start), 0))} - 3px)`,
      onclick: () => { view.sel = on ? null : p.n; view.scroll = -1; draw(); },
    }, h('span.agbadge', { text: String(p.n) }), h('span.agpname.privname', { text: p.name }));
    return tipOn(box, `${p.n}. ${p.name}`, phaseRows(p), { native: true, aria: `Phase ${p.n}, ${took(p.seconds)}: ${on ? 'show every phase' : 'zoom to it'}` });
  }));

  // doing: one segment per step, its label under the long ones
  const strip = h('div.agstrip');
  const labs = h('div.aglabs');
  for (let i = 0; i < st.start.length; i++) {
    const a = x(st.start[i]), b = x(st.end[i]);
    const kind = st.kind[i];
    strip.append(tipOn(h('span', { class: 'agseg', style: `left:${pc(a)};width:max(1px, calc(${pc(b - a)} - 1px));background:${KIND_COLOR[kind]}` }),
      stepTitle(d, i), () => stepRows(d, i), { focus: false }));
    const sec = (st.end[i] - st.start[i]) / 1000;
    if (kind !== 'think' && (b - a) * k >= 0.07) {
      labs.append(h('span.seglab', { style: `left:${pc(a)};width:${pc(b - a)}`, text: sec >= 60 ? `${st.short[i]} ${Math.round(sec / 60)}m` : st.short[i] }));
    }
  }
  const doing = h('div.agdoing', null,
    sel ? h('div.agband', { style: `left:calc(${pc(x(sel.start))} - 3px);width:calc(${pc(x(sel.end) - x(sel.start))} + 3px)` }) : null,
    h('div.agtrack'), strip, labs, gapMarks());

  // context per request, and each request as a dot
  const ticks = niceTicks(Math.max(...R.context, 1), 2);
  const cmax = ticks[ticks.length - 1];
  const Y = (v) => (100 - v / cmax * 96).toFixed(2);
  const line = R.t.map((t, i) => `${i ? 'L' : 'M'}${X(t)} ${Y(R.context[i])}`).join('');
  const area = `${line}L${X(R.t[R.t.length - 1])} 100L${X(R.t[0])} 100Z`;
  const lastCtx = R.context[R.context.length - 1];
  const context = h('div.agchart', { 'aria-label': `Context per request, from ${tokens(R.context[0])} to ${tokens(lastCtx)} tokens, at most ${tokens(T.peak_context)}`, role: 'img' },
    h('span.agmax', { text: tokens(cmax) }), h('div.agmid'), gapMarks(),
    s('svg', { viewBox: `0 0 ${W} 100`, preserveAspectRatio: 'none', 'aria-hidden': 'true' },
      s('path', { d: area, class: 'agctxfill' }),
      s('path', { d: line, class: 'agctxline', 'vector-effect': 'non-scaling-stroke' }),
      s('path', { d: R.t.map((t, i) => `M${X(t)} ${Y(R.context[i])}l0 0.01`).join(''), class: 'agdots', 'vector-effect': 'non-scaling-stroke' })),
    h('span.agend', { style: `top:${Math.min(80, Math.max(0, Number(Y(lastCtx)) - 18)).toFixed(1)}%`, text: tokens(lastCtx) }));
  hoverRequests(context, d, x);

  // spent so far: the running total, a step at each request
  let cum = 0;
  const cums = R.usd.map((v) => (cum += v));
  const umax = niceTicks(Math.max(cum, 1e-6), 2).slice(-1)[0];
  const U = (v) => (100 - v / umax * 96).toFixed(2);
  const spent = R.t.map((t, i) => `${i ? 'L' : 'M'}${X(t)} ${U(i ? cums[i - 1] : 0)}L${X(t)} ${U(cums[i])}`).join('') + `L${W} ${U(cum)}`;
  const spend = h('div.agchart', { role: 'img', 'aria-label': `Spent so far, rising to ${usd(cum)}` },
    h('span.agmax', { text: usdShort(umax) }), h('div.agmid'), gapMarks(),
    s('svg', { viewBox: `0 0 ${W} 100`, preserveAspectRatio: 'none', 'aria-hidden': 'true' },
      s('path', { d: spent, class: 'agcumline', 'vector-effect': 'non-scaling-stroke' })),
    h('span.agend.bold', { style: `top:${Math.min(80, Math.max(0, Number(U(cum)) - 22)).toFixed(1)}%`, text: usd(cum) }));
  hoverRequests(spend, d, x);

  const tl = h('div.agtl', { style: `width:max(640px, calc(112px + (100% - 112px) * ${k.toFixed(4)}))` },
    h('div.lab', { text: 'What it did' }), phases,
    h('div.lab', { text: 'Doing' }), doing,
    h('div.lab', { text: 'Context' }), context,
    h('div.lab', { text: 'Spent so far' }), spend,
    h('div.lab'), timeRow(d, x, k));
  const scroller = h('div.agscroll', null, tl);
  requestAnimationFrame(() => {
    if (!scroller.isConnected) return;
    const plot = scroller.scrollWidth - 112;
    if (view.scroll === -1 && sel) {
      scroller.scrollLeft = Math.max(0, x(sel.start) * plot - (scroller.clientWidth - 112) * 0.1);
      view.scroll = scroller.scrollLeft / Math.max(1, scroller.scrollWidth);
    }
    else if (view.scroll > 0) scroller.scrollLeft = view.scroll * scroller.scrollWidth;
    else view.scroll = 0;
  });

  const zoom = seg('Zoom', ZOOMS.map(([v, t]) => [v, t]), view.sel != null ? '' : view.zoom, (v) => { view.zoom = v; view.sel = null; view.scroll = 0; draw(); });
  const used = KIND_ORDER.filter((kk) => d.kinds.some((r) => r.key === kk && r.steps));
  const legend = h('div.aglegend', null, used.map((kk) => h('span', null, h('i', { style: `background:${KIND_COLOR[kk]}` }), d.kinds.find((r) => r.key === kk).label)),
    h('span', null, h('i.ctx'), 'Context'), gaps.length ? h('span', null, h('i.gap'), 'Waiting for you') : null);
  const noteBits = [d.todo
    ? "Phase names are the agent's own task list, as it started and finished each item; a stretch with no task is named from the commands and files it touched, in gray italics."
    : 'This thread kept no task list: each phase is named from the commands and files it touched, in gray italics.',
  "A step's cost is the request that read its result: a long wait costs little unless its cache expired.",
  'A command\'s time runs until its result came back, so it includes any permission prompt or anything the command waited on.'];
  if (T.is_main && gaps.length) noteBits.push('Waits for you longer than 5 minutes are squeezed to a sliver.');
  return h('section.card.agcard', null,
    h('div.aghrow', null, h('h2', { text: T.is_main ? 'From the first prompt to the last reply' : 'From spawn to hand-back' }), zoom),
    legend, scroller, h('p.note.agnote', { text: noteBits.join(' ') }));
}

function phaseRows(p) {
  return [
    { label: 'When', value: fromTo(p.start, p.end) },
    { label: 'Took', value: took(p.seconds) },
    { label: 'Cost', value: usd(p.usd) },
    ...KIND_ORDER.filter((kk) => p.kinds[kk] >= 1).map((kk) => ({ label: KIND_DOING[kk], value: took(p.kinds[kk]), color: KIND_COLOR[kk] })),
  ];
}

function stepTitle(d, i) {
  const ph = d.steps.phase[i];
  return d.steps.label[i] + (ph != null ? ` · phase ${ph + 1}` : '');
}

const MOVED_NOTE = 'Its cost is counted on the step whose result it read.';
// a step's cost cell: "–" with a hover when its request's cost is counted on another step
function costCell(st, i) {
  if (st.moved && st.moved[i]) return h('td.num', { text: '–', title: MOVED_NOTE });
  return h('td.num', { text: usd(st.usd[i]) });
}

function stepRows(d, i) {
  const st = d.steps, R = d.requests;
  const rows = [{ label: fromTo(st.start[i], st.end[i]), value: took((st.end[i] - st.start[i]) / 1000) }];
  if (st.calls[i] > 1) rows.push({ label: 'Tool calls', value: String(st.calls[i]) });
  const j = st.next[i];
  if (j != null && R.t[j] != null) {
    rows.push({ label: st.kind[i] === 'think' ? 'Its request' : 'Next request', value: `${usd(R.usd[j])}, ${tokens(R.cache_read[j])} read from cache` });
    if (R.cache_write[j] >= 1000) rows.push({ label: 'Cache written', value: tokens(R.cache_write[j]) });
  } else {
    rows.push(st.moved && st.moved[i] ? { label: 'Cost', value: `– ${MOVED_NOTE.toLowerCase()}` } : { label: 'Cost', value: usd(st.usd[i]) });
  }
  if (st.kind[i] !== 'think' && st.kind[i] !== 'files' && (st.end[i] - st.start[i]) >= 60e3) rows.push({ note: WAIT_NOTE });
  return rows;
}

// A tooltip for the request nearest the pointer, on a chart lane.
function hoverRequests(el, d, x) {
  const R = d.requests;
  const xs = R.t.map((t) => x(t));
  el.addEventListener('pointermove', (e) => {
    if (e.target.closest && e.target.closest('.aggap')) return;
    const box = el.getBoundingClientRect();
    const f = (e.clientX - box.left) / box.width;
    let lo = 0, hi = xs.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (xs[mid] < f) lo = mid + 1; else hi = mid; }
    const i = lo > 0 && Math.abs(xs[lo - 1] - f) < Math.abs(xs[lo] - f) ? lo - 1 : lo;
    if (Math.abs(xs[i] - f) * box.width > 10) { hideTip(); return; }
    showTip(e, `Request ${i + 1} at ${at(R.t[i])}`, [
      { label: 'Context', value: tokens(R.context[i]) },
      { label: 'Cost', value: usd(R.usd[i]) },
      { label: 'Cache written', value: tokens(R.cache_write[i]) },
      { label: 'Output', value: (R.estimated[i] ? '≈ ' : '') + tokens(R.output[i]) },
    ]);
  });
  el.addEventListener('pointerleave', hideTip);
}

// Clock times under the plot: the start, the end, and round minutes between,
// about one per 90 px of a view 1000 px wide.
function timeRow(d, x, k) {
  const T = d.thread;
  const row = h('div.agticks');
  row.append(h('span', { style: 'left:0', text: at(T.first) }));
  const want = Math.max(2, Math.round((multiDay ? 6 : 9) * k));
  const room = (multiDay ? 0.13 : 0.09) / k; // the least distance between two labels
  let prevT = T.first, prevF = 0;
  const removed = x.gaps.reduce((a, g) => a + (g.end - g.start), 0);
  const active = Math.max(T.last - T.first - removed, 1);
  const step = [1, 2, 5, 10, 15, 20, 30, 60, 120, 180, 240, 360, 720].map((m) => m * 60e3).find((m) => active / m <= want) || 1440 * 60e3;
  const edge = 0.05 / k;
  const off = new Date(T.first).getTimezoneOffset() * 60e3;
  for (let t = Math.ceil((T.first - off) / step) * step + off; t < T.last; t += step) {
    if (x.gaps.some((g) => t > g.start && t < g.end)) continue;
    const f = x(t);
    if (f < edge || f > 1 - edge || f - prevF < room || 1 - f < room) continue;
    // the date only where the day changes
    row.append(h('span', { style: `left:${pc(f)};transform:translateX(-50%)`, text: sameDay(t, prevT) ? hm(new Date(t)) : at(t) }));
    prevT = t; prevF = f;
  }
  row.append(h('span', { style: 'right:0', text: sameDay(T.last, prevT) ? hm(new Date(T.last)) : at(T.last) }));
  return row;
}

// ------------------------------------------------------------------ what it did

function mix(kinds, total) {
  let acc = 0;
  const stops = KIND_ORDER.filter((kk) => kinds[kk] > 0).map((kk) => {
    const a = acc; acc += kinds[kk] / total * 100;
    return `${KIND_COLOR[kk]} ${a.toFixed(1)}% ${acc.toFixed(1)}%`;
  });
  return stops.length ? `linear-gradient(90deg,${stops.join(',')})` : 'var(--subtle)';
}

function phasesCard(d, draw) {
  const st = d.steps;
  const body = h('tbody');
  for (const p of d.phases) {
    const on = view.sel === p.n;
    const pick = () => { view.sel = on ? null : p.n; view.scroll = -1; draw(); };
    const total = Object.values(p.kinds).reduce((a, v) => a + v, 0) || 1;
    body.append(h('tr', { class: 'agprow' + (on ? ' on' : ''), onclick: pick },
      h('td', null, h('span.agbadge', { text: String(p.n) })),
      h('td', null, h('button', { type: 'button', class: 'agpbtn privname' + (p.guessed ? ' guessed' : ''), 'aria-expanded': String(on), text: p.name,
        onclick: (e) => { e.stopPropagation(); pick(); } })),
      h('td.when', { text: fromTo(p.start, p.end) }),
      h('td.num', { text: took(p.seconds) }),
      h('td', null, h('div.agmix', { style: `background:${mix(p.kinds, total)}`, title: phaseRows(p).slice(3).map((r) => `${r.label} ${r.value}`).join(', ') })),
      h('td.num', { text: usd(p.usd) })));
    if (!on) continue;
    for (let i = p.first; i <= p.last; i++) {
      if (st.phase[i] !== p.n - 1) continue;
      const open = view.folds.has(i);
      const chev = h('button.agchev', { type: 'button', 'aria-expanded': String(open), 'aria-controls': `agfold-${i}`,
        'aria-label': `${open ? 'Hide' : 'Show'} the details of ${st.label[i]}`, title: open ? 'Hide details' : 'Show details',
        onclick: (e) => { e.stopPropagation(); toggleFold(d, i, draw, true); },
        onkeydown: (e) => { if (e.key === 'Escape' && open) { e.preventDefault(); toggleFold(d, i, draw, true); } } });
      body.append(h('tr', { class: 'agsrow' + (open ? ' open' : ''), onclick: () => toggleFold(d, i, draw, false) }, h('td'),
        h('td.stepname', null, h('span.agstep', { title: st.label[i] }, chev, h('i', { style: `background:${KIND_COLOR[st.kind[i]]}` }), h('span.agname', { text: st.label[i] }))),
        h('td.when', { text: at(st.start[i]) }),
        h('td.num', { text: took((st.end[i] - st.start[i]) / 1000) }),
        h('td'),
        costCell(st, i)));
      if (open) {
        body.append(h('tr.agfoldrow', null, h('td'), h('td', { colspan: '5' },
          h('div.agfold', { id: `agfold-${i}`, onkeydown: (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); toggleFold(d, i, draw, true); } } },
            foldBody(d, i)))));
      }
    }
  }
  return h('section.card.agcard', null,
    h('div.aghrow', null, h('h2', { text: 'What it did' }),
      h('span.note', { text: `${plural(d.phases.length, 'phase')} · click one to see its steps and zoom the timeline to it` })),
    h('div.scroll', null, h('table.data.agtable', null,
      h('thead', null, h('tr', null, ['', 'Phase', 'When', 'Took', 'Doing', 'Cost'].map((c, i) =>
        h('th', { scope: 'col', class: i === 3 || i === 5 ? 'num' : null, style: i === 0 ? 'width:28px' : i === 4 ? 'width:160px' : null, text: c })))),
      body)));
}

// ------------------------------------------------------------------ a step's fold

// A step's fold: a thinking step's request token by token with its cost (numbers
// from the index); a tool step's calls in full. Their text (the command, the file's
// change, the output) is the logs' own: asked of `capcom serve` (/api/tool) only when
// the fold opens, kept only while it is open, and marked .tooltext so Ask Roger
// leaves it out. A page without the server's tool-details meta shows the numbers only.
const SHOWN_LINES = 40;
const KEY_NAMES = { command: 'Command', description: 'Description', file_path: 'File', notebook_path: 'Notebook',
  pattern: 'Pattern', path: 'Path', glob: 'Glob', offset: 'Offset', limit: 'Limit', timeout: 'Timeout',
  run_in_background: 'In background', replace_all: 'Replace all', output_mode: 'Output mode', type: 'Type' };
const MONO_KEYS = new Set(['command', 'pattern', 'glob']);
const toolDetailsOn = () => !!document.querySelector('meta[name="capcom-tool-details"][content="on"]');
const writeToken = () => (document.querySelector('meta[name="capcom-token"]') || {}).content || '';

function toggleFold(d, i, draw, fromButton) {
  if (view.folds.has(i)) view.folds.delete(i);
  else {
    const calls = new Map();
    if (d.steps.calls[i] && toolDetailsOn()) for (const [id] of d.steps.tools[i]) calls.set(id, { state: 'loading', all: false });
    view.folds.set(i, calls);
    for (const id of calls.keys()) loadCall(i, id, d.steps.start[i]);
  }
  draw();
  if (fromButton) { const b = document.querySelector(`[aria-controls="agfold-${i}"]`); if (b) b.focus(); }
}

async function loadCall(i, id, t) {
  const url = `/api/tool/${encodeURIComponent(view.sid)}?agent=${encodeURIComponent(view.aid)}&id=${encodeURIComponent(id)}&t=${Math.round(t)}`;
  let next;
  try {
    const res = await fetch(url, { headers: { Accept: 'application/json', 'X-Capcom-Token': writeToken() }, cache: 'no-store' });
    const body = await res.json().catch(() => ({}));
    next = res.ok ? { state: 'ok', data: body } : { state: res.status === 404 ? 'gone' : 'error', message: body.message || '' };
  } catch {
    next = { state: 'error', message: '' };
  }
  const calls = view.folds.get(i);
  if (!calls || !calls.has(id)) return; // closed meanwhile: the text is dropped
  Object.assign(calls.get(id), next);
  const box = document.getElementById(`agfold-${i}`);
  if (box) box.replaceChildren(...foldBody({ steps: currentSteps, requests: currentRequests }, i));
}
// the thread the open folds belong to (set by foldBody, for a fold that fills in later)
let currentSteps = null, currentRequests = null;

function foldBody(d, i) {
  currentSteps = d.steps; currentRequests = d.requests;
  const st = d.steps;
  if (!st.calls[i]) return [thinkFold(d.requests, st.request[i])];
  const calls = view.folds.get(i) || new Map();
  return st.tools[i].map(([id, name, label, end]) => callFold(i, id, name, label, end != null ? (end - st.start[i]) / 1000 : null, calls.get(id)));
}

function thinkFold(R, j) {
  if (j == null || R.t[j] == null) return h('p.note', { text: 'No request for this step.' });
  const p = R.parts[j] || {};
  const row = (label, n, cost, sub) => h('tr', null, h('th', { scope: 'row', text: label }),
    h('td.num', { text: n == null ? '' : n.toLocaleString('en-US') }),
    h('td.num', { text: cost == null ? '' : usd(cost) }), sub ? h('td.agsub', { text: sub }) : h('td'));
  const rows = [
    row('Input', R.input[j], p.input),
    row('Cache read', R.cache_read[j], p.cache_read),
    row('Cache write, 5 min', R.cache_write_5m[j], p.cache_write_5m),
    row('Cache write, 1 hour', R.cache_write_1h[j], p.cache_write_1h),
    row('Output', R.output[j], p.output, R.estimated[j] ? 'estimated, a lower bound' : null),
    row('of it thinking', R.thinking[j], null, R.thinking[j] ? null : 'not logged'),
  ];
  if (p.web) rows.push(row('Web searches and fetches', null, p.web));
  return h('div.agthink', null,
    h('table.agtok', { 'aria-label': 'Tokens and cost of this request' },
      h('thead', null, h('tr', null, h('th', { scope: 'col', text: modelName(R.model[j]) }), h('th.num', { scope: 'col', text: 'Tokens' }), h('th.num', { scope: 'col', text: 'Cost' }), h('th'))),
      h('tbody', null, rows),
      h('tfoot', null, h('tr', null, h('th', { scope: 'row', text: 'Total' }), h('td'), h('td.num', { text: usd(R.usd[j]) }), h('td')))));
}

function status(r, name) {
  if (r.interrupted) return 'interrupted';
  if (r.error) return r.exit != null ? `exit ${r.exit}` : 'error';
  return name === 'Bash' ? 'exit 0' : 'ok';
}

function callFold(i, id, name, label, seconds, entry) {
  const head = (meta) => h('div.agcallhead', null, h('b', { text: name }), h('span.agcallmeta', { text: meta.filter(Boolean).join(' · ') }));
  const tookText = seconds != null ? took(seconds) : 'no result';
  if (!entry) return h('div.agcall', null, head([label !== name ? label : null, tookText]));
  if (entry.state === 'loading') return h('div.agcall', { 'aria-busy': 'true' }, head([tookText]), h('p.note.agloading', { text: 'Reading the log…' }));
  if (entry.state !== 'ok') {
    const msg = entry.state === 'gone' ? 'This call is no longer in the logs (the file was moved, rewritten or deleted).' : 'The details could not be read.';
    return h('div.agcall', null, head([tookText]), h('p.note', { text: msg }));
  }
  const x = entry.data, r = x.result;
  const meta = r ? [status(r, x.name), r.seconds != null ? took(r.seconds) : tookText] : ['no result in the log'];
  const box = h('div.agcall.tooltext', null, head(meta));
  const kv = h('dl.agkv');
  for (const f of x.input.fields) {
    const v = f.value + (f.cut ? ' …' : '');
    kv.append(h('dt', { text: KEY_NAMES[f.key] || f.key }),
      h('dd', null, MONO_KEYS.has(f.key) || v.includes('\n') ? h('pre.agpre', { text: v }) : h('span', { class: f.key.endsWith('path') ? 'mono' : null, text: v })));
  }
  if (x.input.write) kv.append(h('dt', { text: 'Size' }), h('dd', { text: `${x.input.write.lines.toLocaleString('en-US')} lines, ${kb(x.input.write.bytes)}` }));
  if (kv.children.length) box.append(kv);
  if (x.input.edit) box.append(diff(x.input.edit));
  if (r) box.append(output(r, entry, i));
  return box;
}

const kb = (n) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB` : `${(n / 1048576).toFixed(1)} MB`);

function diff(e) {
  const pre = h('pre.agpre.agdiff');
  for (const hk of e.hunks) {
    if (pre.childNodes.length) pre.append(h('span.agsep', { text: '⋯\n' }));
    for (const l of hk.old ? hk.old.split('\n') : []) pre.append(h('span.del', { text: `- ${l}\n` }));
    for (const l of hk.new ? hk.new.split('\n') : []) pre.append(h('span.add', { text: `+ ${l}\n` }));
  }
  return h('div.agblock', null,
    h('div.agblockhead', null, h('span', { text: 'Change' }), h('span.agcallmeta', { text: `−${e.removed.toLocaleString('en-US')} +${e.added.toLocaleString('en-US')} lines${e.cut ? ', cut' : ''}` })),
    pre);
}

function output(r, entry, i) {
  const lines = r.text ? r.text.replace(/\n$/, '').split('\n') : [];
  const many = lines.length > SHOWN_LINES;
  const shown = entry.all || !many ? lines : lines.slice(0, SHOWN_LINES);
  const block = h('div.agblock', null,
    h('div.agblockhead', null, h('span', { text: 'Output' }), h('span.agcallmeta', { text: r.text ? `${r.lines.toLocaleString('en-US')} line${r.lines === 1 ? '' : 's'}, ${kb(r.bytes)}` : 'empty' })));
  if (r.text) block.append(h('pre.agpre.agout', { tabindex: '0', text: shown.join('\n') }));
  if (many && !entry.all) {
    block.append(h('button.link.agall', { type: 'button', text: r.cut ? `Show all ${lines.length.toLocaleString('en-US')} lines sent` : `Show all ${r.lines.toLocaleString('en-US')} lines`,
      onclick: (e) => { e.stopPropagation(); entry.all = true; const box = document.getElementById(`agfold-${i}`); if (box) box.replaceChildren(...foldBody({ steps: currentSteps, requests: currentRequests }, i)); } }));
  }
  if (r.cut) block.append(h('p.note', { text: `The output is ${kb(r.bytes)}: only its first 200 KB are shown.` }));
  return block;
}

// ------------------------------------------------------------------ where the time went

function timeCard(d, working) {
  const rows = d.kinds.filter((k) => k.steps > 0);
  const total = rows.reduce((a, k) => a + k.seconds, 0) || 1;
  const bar = segBar(rows.map((k) => ({ color: KIND_COLOR[k.key], share: k.seconds / total, title: k.label,
    rows: [{ label: 'Time', value: took(k.seconds) }, { label: 'Share', value: pct(k.seconds / total) }, { label: 'Cost', value: usd(k.usd) }] })), { tall: true, aria: 'Share of time by activity' });
  const table = h('table.data.narrow', null,
    h('thead', null, h('tr', null, ['Activity', 'Time', 'Share', 'Steps', 'Cost'].map((c, i) => h('th', { scope: 'col', class: i ? 'num' : null, text: c })))),
    h('tbody', null, rows.map((k) => h('tr', null,
      h('td.stepname', null, h('span.agstep', { title: k.label }, h('i', { style: `background:${KIND_COLOR[k.key]}` }), h('span.agname', { text: k.label }))),
      h('td.num', { text: took(k.seconds) }),
      h('td.num', { text: pct(k.seconds / total) }),
      h('td.num', { text: k.key === 'think' ? plural(k.steps, 'request') : k.steps.toLocaleString('en-US') }),
      k.moved ? h('td.num', { text: '–', title: MOVED_NOTE }) : h('td.num', { text: usd(k.usd) })))));
  return h('section.card.agcard.agtime', null, h('h2', { text: 'Where the time went' }), bar, h('div.scroll', null, table),
    timeSentence(d, rows, working));
}

// "Tests and builds took over half its life: six pytest runs, three of them over 5 minutes."
function timeSentence(d, rows, working) {
  const top = [...rows].sort((a, b) => b.seconds - a.seconds)[0];
  if (!top || !(working > 0)) return null;
  const of = d.thread.is_main ? 'its working time' : 'its life';
  let text = `${top.label} took ${share(Math.min(1, top.seconds / working))} of ${of}`;
  if (top.key !== 'think') {
    const st = d.steps, by = new Map();
    for (let i = 0; i < st.kind.length; i++) {
      if (st.kind[i] !== top.key) continue;
      const sec = (st.end[i] - st.start[i]) / 1000;
      const e = by.get(st.short[i]) || { n: 0, long: 0, sec: 0 };
      e.n += 1; e.sec += sec; if (sec >= 300) e.long += 1;
      by.set(st.short[i], e);
    }
    const [name, e] = [...by.entries()].sort((a, b) => b[1].sec - a[1].sec)[0] || [];
    if (name && e.n > 1) text += `: ${count(e.n)} ${name} runs` + (e.long ? `, ${count(e.long)} of them over 5 minutes` : '');
    else if (name) text += `, most of it ${name}`;
  }
  return h('p.agline', { text: text + '.' });
}

function slowCard(d, draw) {
  const st = d.steps, n = st.start.length;
  const idx = [...Array(n).keys()];
  const shown = view.all ? idx.slice(0, ROWS_MAX) : idx.sort((a, b) => (st.end[b] - st.start[b]) - (st.end[a] - st.start[a])).slice(0, 6);
  const phaseName = (i) => (st.phase[i] != null && d.phases[st.phase[i]] ? d.phases[st.phase[i]].name : '');
  const table = h('table.data.narrow', null,
    h('thead', null, h('tr', null, ['When', 'Phase', 'Step', 'Took', view.all ? 'Cost' : 'Next request'].map((c, i) => h('th', { scope: 'col', class: i >= 3 ? 'num' : null, text: c })))),
    h('tbody', null, shown.map((i) => h('tr', null,
      h('td.when', { text: at(st.start[i]) }),
      h('td', null, st.phase[i] != null ? tipOn(h('span.agbadge', { text: String(st.phase[i] + 1) }), `Phase ${st.phase[i] + 1}`, [{ note: phaseName(i) }], { focus: false }) : null),
      h('td.stepname', null, h('span.agstep', { title: st.label[i] }, h('i', { style: `background:${KIND_COLOR[st.kind[i]]}` }), h('span.agname', { text: st.label[i] }))),
      h('td.num', { text: took((st.end[i] - st.start[i]) / 1000) }),
      costCell(st, i)))));
  const more = n > 6 ? h('button.link.agmore', { type: 'button', onclick: () => { view.all = !view.all; draw(); },
    text: view.all ? 'Only the slowest' : `All ${n.toLocaleString('en-US')} steps` }) : null;
  const capped = view.all && n > ROWS_MAX ? h('p.note', { text: `The first ${ROWS_MAX.toLocaleString('en-US')} steps are listed.` }) : null;
  return h('section.card.agcard.agslow', null, h('h2', { text: view.all ? 'Every step' : 'Slowest steps' }), h('div.scroll', null, table), capped, more);
}
