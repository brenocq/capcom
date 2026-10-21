// Spikes: the jumps in spend of a window, the extra cost by cause, and the selected
// spike in detail (its cause, the bars of its window, the evidence, what to change).

import {
  h, usd, stamp, hm, monthDay, seg, empty, setQuery, sessionHref, maxOf, spikeText, daysText, CONFIDENCE,
} from './lib.js';
import { pendingBlock } from './sessions.js';

const RANGES = [[1, 'Today'], [7, '7 days'], [14, '14 days'], [30, '30 days'], [90, '90 days']];

function when(ms) { const d = new Date(ms); return `${monthDay(d)}, ${hm(d)}`; }

function thread(it) {
  if (it.scope === 'session') return 'session-wide';
  return it.agent_id ? `subagent${it.agent_type ? ' ' + it.agent_type : ''}` : 'main thread';
}

function where(it) {
  return [it.title || it.project || (it.session_id ? it.session_id.slice(0, 8) : 'all sessions'), thread(it)].join(' · ');
}

export async function renderSpikes(main, r) {
  const days = Number(r.query.get('days')) || 14;
  const head = h('header.head', null,
    h('div.titles', null, h('h1', { text: 'Spikes' })),
    h('div.controls', null, seg('Range', RANGES, days, (v) => setQuery({ days: v, spike: null }))));
  main.append(head);
  const { data: d, block } = await pendingBlock('spikes', `days=${days}`);
  if (block) {
    main.append(h('section.card', null, block));
    return;
  }
  const titles = head.firstChild;
  if (!d.count) {
    titles.append(h('p.sub', { text: `No spikes in ${daysText(days)}: the spend rose at its usual pace.` }));
    main.append(h('section.card', null, empty('Nothing jumped', 'A spike is a request far above its recent median, or a rate that rose and stayed up. None did in this window.')));
    return;
  }
  const times = d.count === 1 ? 'Once' : `${d.count} times`;
  titles.append(h('p.sub', null,
    `${times} in ${daysText(days)} the spend jumped or its slope rose and stayed up. capcom matched ${d.explained} to a cause in the logs. Together they cost `,
    h('strong', { text: usd(d.excess_usd) }), ' more than your usual pace.'));

  // extra cost by cause
  if (d.causes.length) {
    const max = maxOf(d.causes.map((c) => c.excess_usd), 1e-9);
    main.append(h('section.card', null, h('h2', { text: 'Extra cost by cause' }),
      h('div.causes', null, d.causes.map((c) => h('div.cause', null,
        h('span', { text: c.label }),
        h('div.track', null, h('div', { style: `width:${(c.excess_usd / max * 100).toFixed(1)}%;background:${c.cause === 'unexplained' ? 'var(--gray)' : 'var(--accent)'}` })),
        h('span.mono', { text: usd(c.excess_usd) }))))));
  }

  // the list and the selected spike
  const items = d.spikes;
  const largest = items.reduce((a, b) => (b.excess_usd > a.excess_usd ? b : a), items[0]);
  const wanted = r.query.get('spike');
  const sel = items.find((it) => it.key === wanted) || largest;
  const list = h('section.card.spikelist', { 'aria-label': 'Spikes' }, items.map((it) => {
    const txt = spikeText(it);
    return h('button', { type: 'button', 'aria-pressed': String(it === sel), onclick: () => setQuery({ spike: it.key }) },
      h('span', { class: txt.unexplained || !it.cause ? 'n gray' : 'n', text: String(it.n) }),
      h('span.body', null,
        h('span.row', null, h('span.t', { text: txt.title }), h('span.plus', { text: '+' + usd(it.excess_usd) })),
        h('span.meta', { text: `${when(it.start)} · ${where(it)}` })));
  }));
  main.append(h('div.row-wrap', { style: 'align-items:flex-start' }, list, detail(sel)));
  const hidden = d.count - items.length;
  if (wanted && !items.some((it) => it.key === wanted)) {
    main.append(h('p.note', { style: 'margin:0', text: 'The spike you followed is not among the largest of this window; the largest is shown.' }));
  }
  if (hidden > 0) main.append(h('p.note', { style: 'margin:0', text: `The ${items.length} largest of ${d.count} spikes are listed; the causes above count them all.` }));
}

function detail(it) {
  const txt = spikeText(it);
  const causes = it.explanations || [];
  const top = causes.length ? causes.reduce((a, b) => (b.excess_usd > a.excess_usd ? b : a)) : null;
  const conf = top ? top.confidence : null;
  const box = h('section.card.detail');
  box.append(h('div.tags', null,
    h('span.tag', { text: txt.tag }),
    conf ? h('span', { class: `tag conf ${conf}`, text: top.confidence_label || CONFIDENCE[conf] || conf }) : null,
    it.scope === 'session' ? h('span.tag', { text: 'session-wide', title: 'Found over all the threads of the session together, where a fan-out of subagents shows.' }) : null,
    h('span.note', { text: `${when(it.start)} · ${where(it)}` })));
  box.append(h('div.split', { style: 'align-items:baseline' }, h('h2.big2', { text: txt.title }), h('span.excess', { text: '+' + usd(it.excess_usd) })));
  const body = top && top.summary ? top.summary
    : `${it.requests} request${it.requests === 1 ? '' : 's'} cost ${usd(it.cost_usd)}, where the usual pace would have spent ${usd(it.baseline_usd)}.`;
  box.append(h('p.para', { text: body }));
  const others = causes.filter((c) => c !== top);
  if (others.length) {
    box.append(h('div.also', null, h('span.label', { text: 'Also behind it' }),
      others.map((c) => h('span', null, h('b', { text: c.cause_label }), ` ${c.title} `, h('span.plus', { text: '+' + usd(c.excess_usd) })))));
  }
  if (it.window) box.append(windowChart(it));

  const evidence = causes.flatMap((c) => c.evidence);
  const facts = h('div.facts', null, h('h3', { text: 'Evidence in the logs' }),
    evidence.length ? h('ul.evidence', null, evidence.map((e) => h('li', null, h('span', { text: e.label }))))
      : h('p.note', { text: causes.length ? 'No log record was cited.' : 'Causes are matched from milestone 3.' }));
  const advice = causes.map((c) => c.advice).filter(Boolean);
  const fix = h('div.fix', null, h('h3', { text: 'What to change' }),
    advice.length ? advice.map((a) => h('p', { text: a })) : h('p', { text: 'Nothing to change yet. If this pattern comes back, look at the session around this time.' }),
    it.session_id ? h('a.link', { href: sessionHref(it.session_id), text: 'Open the session' }) : null);
  box.append(h('div.row-wrap', { style: 'gap:24px' }, facts, fix));
  return box;
}

// Bars around the spike: the usual pace in gray, the extra above it in the accent.
function windowChart(it) {
  const w = it.window;
  const n = w.usual.length;
  const totals = w.usual.map((u, i) => u + w.extra[i]);
  const ymax = maxOf(totals, 1e-6) * 1.1;
  const H = 180;
  const chart = h('div.wbars', { style: `height:${H}px`, role: 'img', 'aria-label': `Spend in ${w.step_s / 60}-minute bars around the spike: ${usd(it.excess_usd)} above the usual pace.` });
  chart.append(h('span.ylab', { style: 'top:0', text: usd(ymax, ymax < 1 ? 2 : 0) }), h('span.ylab', { style: 'bottom:0', text: '$0' }));
  const bars = h('div.bars');
  for (let i = 0; i < n; i++) {
    const t = w.start + i * w.step_s * 1000;
    const bar = h('div', { title: `${hm(new Date(t))}: ${usd(w.usual[i])} at the usual pace${w.extra[i] > 0 ? `, +${usd(w.extra[i])} extra` : ''}` },
      h('div', { style: `height:${(w.usual[i] / ymax * 100).toFixed(2)}%;background:var(--gray)` }),
      w.extra[i] > 0 ? h('div', { style: `height:${(w.extra[i] / ymax * 100).toFixed(2)}%;background:var(--accent);border-radius:2px 2px 0 0` }) : null);
    bars.append(bar);
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
    h('div.wx', null, h('span', { text: stamp(new Date(w.start)) }), h('span', { text: step }), h('span', { text: stamp(new Date(w.start + span)) })));
}
