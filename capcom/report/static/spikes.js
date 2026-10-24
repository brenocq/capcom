// Spikes: the jumps in spend of a window, the extra cost by cause, and the selected
// spike in detail (its cause, the bars of its window, the evidence, what to change).

import {
  h, usd, stamp, hm, monthDay, seg, empty, setQuery, sessionHref, maxOf, spikeText, daysText, CONFIDENCE, projectChip,
  pct, tipOn, tableToggle, sessionDot,
} from './lib.js';
import { pendingBlock } from './sessions.js';

const RANGES = [[1, 'Today'], [7, '7 days'], [14, '14 days'], [30, '30 days'], [90, '90 days']];

// The list's order, kept in the URL (?sort=cost-desc|cost-asc|date-desc|date-asc):
// sorting is client-side, so the API never sees it.
export const SORTS = ['cost-desc', 'cost-asc', 'date-desc', 'date-asc'];
export function parseSort(v) { return SORTS.includes(v) ? v : 'cost-desc'; }
export function sortText(sort) {
  return {
    'cost-desc': 'Sorted by cost, highest first', 'cost-asc': 'Sorted by cost, lowest first',
    'date-desc': 'Sorted by date, newest first', 'date-asc': 'Sorted by date, oldest first',
  }[parseSort(sort)];
}
// A sorted copy; ties keep the chart's marker order.
export function sortSpikes(items, sort) {
  const [key, dir] = parseSort(sort).split('-');
  const val = (it) => (key === 'cost' ? it.excess_usd : new Date(it.start).getTime());
  const sign = dir === 'asc' ? 1 : -1;
  return [...items].sort((a, b) => sign * (val(a) - val(b)) || a.n - b.n);
}

// Once the redrawn page is in the document: the selected row scrolled into view
// inside the list (never the page), and after a sort click its control focused again.
let afterSort = null;
function settle(list, controls) {
  const job = afterSort;
  afterSort = null;
  const run = (tries) => {
    if (!list.isConnected) { if (tries) requestAnimationFrame(() => run(tries - 1)); return; }
    const btn = job ? controls.querySelector(job) : null;
    if (btn) btn.focus({ preventScroll: true });
    const on = list.querySelector('button[aria-pressed="true"]');
    if (on) list.scrollTop = on.offsetTop - list.offsetTop - (list.clientHeight - on.offsetHeight) / 2;
  };
  requestAnimationFrame(() => run(30));
}

function when(ms) { const d = new Date(ms); return `${monthDay(d)}, ${hm(d)}`; }

function thread(it) {
  if (it.scope === 'session') return 'session-wide';
  return it.agent_id ? `subagent${it.agent_type ? ' ' + it.agent_type : ''}` : 'main thread';
}

function where(it) {
  return [it.title || (it.session_id ? it.session_id.slice(0, 8) : 'all sessions'), thread(it)].join(' · ');
}

export async function renderSpikes(main, r) {
  const days = Number(r.query.get('days')) || 14;
  const project = r.query.get('project') || '';
  const cause = r.query.get('cause') || '';
  const head = h('header.head', null,
    h('div.titles', null, h('h1', { text: 'Spikes' })),
    h('div.controls', null, seg('Range', RANGES, days, (v) => setQuery({ days: v, spike: null }))));
  main.append(head);
  const q = new URLSearchParams({ days: String(days) });
  if (project) q.set('project', project);
  if (cause) q.set('cause', cause);
  const { data: d, block } = await pendingBlock('spikes', q.toString());
  if (block) {
    main.append(h('section.card', null, block));
    return;
  }
  const titles = head.firstChild;
  const pick = (p) => setQuery({ project: p, spike: null });
  const pickCause = (c) => setQuery({ cause: c, spike: null });
  if (d.project) {
    titles.append(h('div.chips', null, h('span.note', { text: 'Project' }),
      h('button.tag.proj.filter', { type: 'button', title: `${d.project_path || d.project}: show every project`, 'aria-label': `Remove the filter on project ${d.project}`, onclick: () => pick(null) },
        d.project, h('span', { 'aria-hidden': 'true', text: ' ×' }))));
  }
  // filter chips, one per cause of the window, in its color
  if (d.causes.length > 1 || d.cause) {
    main.append(h('div.chips.causechips', null, h('span.note', { text: 'Cause' }),
      d.causes.map((c) => {
        const on = d.cause === c.cause;
        return h('button.tag.filter.causechip', { type: 'button', 'aria-pressed': String(on), title: on ? 'Show every cause' : `Show the spikes ${c.label.toLowerCase()} explains`, onclick: () => pickCause(on ? null : c.cause) },
          h('i', { style: `background:${c.color || 'var(--gray)'}` }), c.label, on ? h('span', { 'aria-hidden': 'true', text: ' ×' }) : null);
      })));
  }
  const inProject = (d.project ? ` in ${d.project}` : '') + (d.cause ? ` with ${(d.causes.find((c) => c.cause === d.cause) || { label: d.cause }).label.toLowerCase()}` : '');
  if (!d.count) {
    titles.append(h('p.sub', { text: `No spikes${inProject} in ${daysText(days)}: the spend rose at its usual pace.` }));
    main.append(h('section.card', null, empty('Nothing jumped', 'A spike is a request far above its recent median, or a rate that rose and stayed up. None did in this window.')));
    return;
  }
  const times = d.count === 1 ? 'Once' : `${d.count} times`;
  titles.append(h('p.sub', null,
    `${times}${inProject} in ${daysText(days)} the spend jumped or its slope rose and stayed up. capcom matched ${d.explained} to a cause in the logs. Together they cost `,
    h('strong', { text: usd(d.excess_usd) }), ' more than your usual pace.'));

  // extra cost by cause, and by project (a project row filters the page to it)
  const bars = [];
  if (d.causes.length) {
    const max = maxOf(d.causes.map((c) => c.excess_usd), 1e-9);
    const causeSum = d.causes.reduce((a, c) => a + c.excess_usd, 0);
    bars.push(h('section.card.grow1', null, h('h2', { text: d.project ? `Extra cost by cause in ${d.project}` : 'Extra cost by cause' }),
      h('div.causes.one.hl', null, d.causes.map((c) => {
        const on = d.cause === c.cause;
        const n = `${c.explanations} explanation${c.explanations === 1 ? '' : 's'}`;
        const act = on ? 'show every cause' : 'show these spikes only';
        return tipOn(h('button.cause.pick', { type: 'button', 'aria-pressed': String(on), onclick: () => pickCause(on ? null : c.cause) },
          h('span.plabel', { text: c.label }),
          h('div.track', null, h('div', { style: `width:${(c.excess_usd / max * 100).toFixed(1)}%;background:${d.cause && !on ? 'var(--gray)' : (c.color || 'var(--gray)')}` })),
          h('span.mono', { text: usd(c.excess_usd) })),
        c.label, [
          { label: 'Extra cost', value: usd(c.excess_usd), color: c.color || 'var(--gray)' },
          causeSum > 0 ? { label: 'Share of the extra', value: pct(c.excess_usd / causeSum) } : null,
          { label: 'Explanations', value: String(c.explanations) },
          { note: `Click to ${act}.` },
        ].filter(Boolean), { native: true, aria: `${c.label}: ${usd(c.excess_usd)} extra, ${n}; ${act}` });
      }))));
  }
  const byProject = d.by_project || [];
  if (byProject.length) {
    const max = maxOf(byProject.map((p) => p.excess_usd), 1e-9);
    const projSum = byProject.reduce((a, p) => a + p.excess_usd, 0);
    bars.push(h('section.card.grow1', null, h('h2', { text: 'Extra cost by project' }),
      h('div.causes.one.hl', null, byProject.map((p) => {
        const on = d.project && p.project === d.project;
        const label = p.project || 'no session';
        const row = [
          h('span.plabel', { text: label }),
          h('div.track', null, h('div', { style: `width:${(p.excess_usd / max * 100).toFixed(1)}%;background:${d.project && !on ? 'var(--gray)' : 'var(--accent)'}` })),
          h('span.mono', { text: usd(p.excess_usd) })];
        const n = `${p.spikes} spike${p.spikes === 1 ? '' : 's'}`;
        const act = on ? 'show every project' : 'show its spikes only';
        const rows = [
          { label: 'Extra cost', value: usd(p.excess_usd), color: 'var(--accent)' },
          projSum > 0 ? { label: 'Share of the extra', value: pct(p.excess_usd / projSum) } : null,
          { label: 'Spikes', value: String(p.spikes) },
          p.project_path && p.project_path !== label ? { note: p.project_path } : null,
          p.project ? { note: `Click to ${act}.` } : null,
        ].filter(Boolean);
        if (!p.project) return tipOn(h('div.cause', null, ...row), label, rows);
        return tipOn(h('button.cause.pick', { type: 'button', 'aria-pressed': String(Boolean(on)), onclick: () => pick(on ? null : p.project) }, ...row),
          label, rows, { native: true, aria: `${label}: ${usd(p.excess_usd)} extra, ${n}; ${act}` });
      }))));
  }
  if (bars.length) main.append(h('div.row-wrap', null, ...bars));

  // the list and the selected spike
  const sort = parseSort(r.query.get('sort'));
  const [sortKey, sortDir] = sort.split('-');
  const items = sortSpikes(d.spikes, sort);
  const setSort = (v, job) => { afterSort = job; setQuery({ sort: v === 'cost-desc' ? null : v }); };
  const controls = h('div.sortbar', null,
    seg('Sort by', [['cost', 'Cost'], ['date', 'Date']], sortKey, (k) => setSort(`${k}-${sortDir}`, `[data-sort="${k}"]`)),
    h('button.sortdir', {
      type: 'button', 'data-sort': 'dir', 'aria-pressed': String(sortDir === 'asc'),
      'aria-label': sortDir === 'desc' ? 'Sort descending' : 'Sort ascending',
      title: sortDir === 'desc' ? 'Highest or newest first: show the lowest or oldest first' : 'Lowest or oldest first: show the highest or newest first',
      onclick: () => setSort(`${sortKey}-${sortDir === 'desc' ? 'asc' : 'desc'}`, '[data-sort="dir"]'),
    }, h('span', { 'aria-hidden': 'true', text: sortDir === 'desc' ? '↓' : '↑' })),
    h('span.note.sortstatus', { role: 'status', 'aria-live': 'polite', text: sortText(sort) }));
  for (const b of controls.querySelectorAll('.seg button')) b.dataset.sort = b.textContent.toLowerCase();
  // the default selection: the window's largest spike
  const largest = items.find((it) => it.key === d.largest) || items.reduce((a, b) => (b.excess_usd > a.excess_usd ? b : a), items[0]);
  const wanted = r.query.get('spike');
  const sel = items.find((it) => it.key === wanted) || largest;
  const list = h('section.card.spikelist', { 'aria-label': 'Spikes' }, items.map((it) => {
    const txt = spikeText(it);
    return h('button', { type: 'button', 'aria-pressed': String(it === sel), onclick: () => setQuery({ spike: it.key }) },
      h('span', { class: txt.unexplained || !it.cause ? 'n gray' : 'n', text: String(it.n), style: it.cause_color && !txt.unexplained ? `background:${it.cause_color};color:#fff` : null }),
      h('span.body', null,
        h('span.row', null, h('span.t', { text: txt.title }), h('span.plus', { text: '+' + usd(it.excess_usd) })),
        txt.rest ? h('span.rest', { text: txt.rest }) : null,
        h('span.meta', null, projectChip(it), h('span', null, `${when(it.start)} · `, sessionDot(it.color), where(it)))));
  }));
  main.append(h('div.row-wrap', { style: 'align-items:flex-start' }, h('div.spikecol', null, controls, list), detail(sel)));
  settle(list, controls);
  const hidden = d.count - items.length;
  if (wanted && !items.some((it) => it.key === wanted)) {
    main.append(h('p.note', { style: 'margin:0', text: 'The spike you followed is not among the largest of this window; the largest is shown.' }));
  }
  if (hidden > 0) main.append(h('p.note', { style: 'margin:0', text: `The ${items.length} largest of ${d.count} spikes are listed; the causes above count them all.` }));
}

// What to change: the advice of the named causes only; the unexplained rest's advice
// ("nothing to change yet") shows only when no cause was named.
export function spikeAdvice(causes) {
  const named = causes.filter((c) => c.cause !== 'unexplained');
  const advice = (named.length ? named : causes).map((c) => c.advice).filter(Boolean);
  return advice.length ? advice : ['Nothing to change yet. If this pattern comes back, look at the session around this time.'];
}

function detail(it) {
  const txt = spikeText(it);
  const causes = it.explanations || [];
  const named = causes.filter((c) => c.cause !== 'unexplained');
  const pool = named.length ? named : causes;
  const top = pool.length ? pool.reduce((a, b) => (b.excess_usd > a.excess_usd ? b : a)) : null;
  const conf = top ? top.confidence : null;
  const box = h('section.card.detail');
  if (it.session_id) box.dataset.askSession = it.session_id; // for Ask Roger's facts (ask.js)
  box.append(h('div.tags', null,
    projectChip(it),
    txt.same ? null : h('span.tag', { text: txt.tag }),
    conf ? h('span', { class: `tag conf ${conf}`, text: top.confidence_label || CONFIDENCE[conf] || conf }) : null,
    it.scope === 'session' ? h('span.tag', { text: 'session-wide', title: 'Found over all the threads of the session together, where a fan-out of subagents shows.' }) : null,
    h('span.note', { text: `${when(it.start)} · ${where(it)}` })));
  box.append(h('div.split', { style: 'align-items:baseline' }, h('h2.big2', { text: txt.title }), h('span.excess', { text: '+' + usd(it.excess_usd) })));
  if (txt.rest) box.append(h('p.note', { style: 'margin:0', text: txt.rest[0].toUpperCase() + txt.rest.slice(1) + '.' }));
  const body = top && top.summary ? top.summary
    : `${it.requests} request${it.requests === 1 ? '' : 's'} cost ${usd(it.cost_usd)}, where the usual pace would have spent ${usd(it.baseline_usd)}.`;
  box.append(h('p.para', { text: body }));
  const others = causes.filter((c) => c !== top);
  if (others.length) {
    box.append(h('div.also', null, h('span.label', { text: 'Also behind it' }),
      others.map((c) => h('span', null, h('b', { text: c.title && c.title.toLowerCase().startsWith(c.cause_label.toLowerCase()) ? c.title : c.cause_label }), c.title && !c.title.toLowerCase().startsWith(c.cause_label.toLowerCase()) ? ` ${c.title} ` : ' ', h('span.plus', { text: '+' + usd(c.excess_usd) })))));
  }
  if (it.window) box.append(windowChart(it));

  const evidence = causes.flatMap((c) => c.evidence);
  const facts = h('div.facts', null, h('h3', { text: 'Evidence in the logs' }),
    // every row with its date: its own record's, else the spike's start
    evidence.length ? h('ul.evidence', null, evidence.map((e) => h('li', null, h('span', { text: e.label }),
      h('span.note', { text: ' · ' + when(e.timestamp || it.start) }))))
      : h('p.note', { text: causes.length ? 'No log record was cited.' : it.explanations ? 'No cause was matched.' : 'Causes are matched from milestone 3.' }));
  const fix = h('div.fix', null, h('h3', { text: 'What to change' }),
    spikeAdvice(causes).map((a) => h('p', { text: a })),
    it.session_id ? h('a.link', { href: sessionAt(it), text: 'Open the session' }) : null);
  box.append(h('div.row-wrap', { style: 'gap:24px' }, facts, fix));
  return box;
}

// The spike's session, its timeline opened on the spike (`at` to `to`, shaded there)
function sessionAt(it) {
  const iso = (t) => new Date(t).toISOString();
  const q = new URLSearchParams({ at: iso(it.start), to: iso(it.end || it.start) });
  return sessionHref(it.session_id) + '?' + q;
}

// Bars around the spike: the usual pace in gray, the extra above it in the accent.
function windowChart(it) {
  // ISO times from the API, epoch ms for the bar arithmetic
  const w = { ...it.window, start: Date.parse(it.window.start), spike_start: Date.parse(it.window.spike_start), spike_end: Date.parse(it.window.spike_end) };
  const n = w.usual.length;
  const totals = w.usual.map((u, i) => u + w.extra[i]);
  const ymax = maxOf(totals, 1e-6) * 1.1;
  const H = 180;
  const chart = h('div.wbars', { style: `height:${H}px`, role: 'img', 'aria-label': `Spend in ${w.step_s / 60}-minute bars around the spike: ${usd(it.excess_usd)} above the usual pace.` });
  chart.append(h('span.ylab', { style: 'top:0', text: usd(ymax, ymax < 1 ? 2 : 0) }), h('span.ylab', { style: 'bottom:0', text: '$0' }));
  const bars = h('div.bars.hl');
  for (let i = 0; i < n; i++) {
    const t = w.start + i * w.step_s * 1000;
    const bar = h('div', null,
      h('div', { style: `height:${(w.usual[i] / ymax * 100).toFixed(2)}%;background:var(--gray)` }),
      w.extra[i] > 0 ? h('div', { style: `height:${(w.extra[i] / ymax * 100).toFixed(2)}%;background:var(--accent);border-radius:2px 2px 0 0;margin-bottom:2px` }) : null);
    bars.append(tipOn(bar, `${hm(new Date(t))}: ${usd(totals[i])}`, [
      { label: 'Usual pace', value: usd(w.usual[i]), color: 'var(--gray)' },
      w.extra[i] > 0 ? { label: 'Extra', value: '+' + usd(w.extra[i]), color: 'var(--accent)' } : null,
    ].filter(Boolean), { focus: false }));
  }
  chart.append(bars);
  const span = n * w.step_s * 1000;
  const left = ((w.spike_start - w.start) / span * 100).toFixed(2);
  chart.append(h('div.wmark', { style: `left:${left}%` }), h('span.wlab', { style: `left:${left}%`, text: spikeText(it).tag.toLowerCase() }));
  const step = w.step_s >= 3600 ? `${w.step_s / 3600}-hour bars` : `${w.step_s / 60}-minute bars`;
  return h('div.wchart', null,
    h('div.legend', null,
      h('span', null, h('i', { style: 'background:var(--gray)' }), 'usual pace'),
      h('span', null, h('i', { style: 'background:var(--accent)' }), 'extra, above the baseline')),
    chart,
    h('div.wx', null, h('span', { text: stamp(new Date(w.start)) }), h('span', { text: step }), h('span', { text: stamp(new Date(w.start + span)) })),
    tableToggle(['Time', 'Usual pace', 'Extra'], w.usual.map((u, i) => [hm(new Date(w.start + i * w.step_s * 1000)), usd(u), w.extra[i] > 0 ? '+' + usd(w.extra[i]) : ''])));
}
